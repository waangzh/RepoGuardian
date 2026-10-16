import os
import subprocess
import sys
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.core import database
from app.models.orm import ArtifactOrm, utcnow
from app.models.review import ReviewTask
from app.services.artifact_store import LocalArtifactStore
from app.services.review_repository import ReviewRepository
from test_runtime_integrity_errors import runtime_payload


@pytest.mark.parametrize("revision,expected", [(None, False), ("20260804_0002", False),
                                              ("20261016_0003", False), ("20261016_0004", True)])
def test_schema_gate_requires_artifact_migration(tmp_path, monkeypatch, revision, expected):
    engine = create_engine(f"sqlite:///{(tmp_path / 'gate.db').as_posix()}")
    with engine.begin() as connection:
        if revision is not None:
            connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
            connection.execute(text("INSERT INTO alembic_version VALUES (:revision)"),
                               {"revision": revision})
    monkeypatch.setattr(database, "sync_engine", engine)
    try:
        assert database.schema_is_current() is expected
    finally:
        engine.dispose()


def migrate(path, target, *, direction="upgrade"):
    return subprocess.run(
        [sys.executable, "-m", "alembic", direction, target],
        cwd=Path(__file__).parents[1],
        env={**os.environ, "REPOGUARDIAN_DB_PATH": str(path)},
        check=True, capture_output=True, text=True, encoding="utf-8",
    )


@pytest.mark.parametrize("legacy", [True, False], ids=["historical-schema", "current-schema"])
def test_upgrade_preserves_artifacts_and_allows_same_content_across_tasks(tmp_path, legacy):
    path = tmp_path / "upgrade.db"
    migrate(path, "20260804_0002")
    engine = create_engine(f"sqlite:///{path.as_posix()}")

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    sessions = sessionmaker(engine)
    repository = ReviewRepository(sessions, LocalArtifactStore(tmp_path / "artifacts"),
                                  require_migration=False)
    try:
        if legacy:
            with engine.begin() as connection:
                operations = Operations(MigrationContext.configure(connection))
                with operations.batch_alter_table("artifacts") as batch:
                    batch.create_unique_constraint("uq_artifacts_hash_kind", ["sha256", "kind"])
        for task_id in ("first", "second"):
            repository.create_task(ReviewTask(id=task_id, pr_url="https://github.com/o/r/pull/1",
                                             engine="task_review_v1"))
        references = [asdict(repository._artifacts.put_text(
            task_id=task_id, kind="derivation", content='{"candidates": {}}'))
            for task_id in ("first", "second")]
        first = runtime_payload("first", references[0])
        second = runtime_payload("second", references[1])
        repository.save_task_review_runtime("first", first, -1)
        if legacy:
            with pytest.raises(IntegrityError, match="SQLITE_CONSTRAINT_UNIQUE"):
                repository.save_task_review_runtime("second", second, -1)
        with engine.connect() as connection:
            before = connection.exec_driver_sql("SELECT * FROM artifacts ORDER BY id").all()
        original_indexes = inspect(engine).get_indexes("artifacts")

        migrate(path, "head")

        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT * FROM artifacts ORDER BY id").all() == before
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert connection.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
        assert inspect(engine).get_indexes("artifacts") == original_indexes
        assert [item["column_names"] for item in inspect(engine).get_unique_constraints(
            "artifacts")] == [["uri"]]
        foreign = inspect(engine).get_foreign_keys("artifacts")
        assert foreign[0]["constrained_columns"] == ["task_id"]
        assert foreign[0]["referred_table"] == "review_tasks"
        assert foreign[0]["options"]["ondelete"] == "CASCADE"

        repository.save_task_review_runtime("second", second, -1)
        assert references[0]["sha256"] == references[1]["sha256"]
        assert references[0]["uri"] != references[1]["uri"]
        assert repository.load_task_review_runtime("first") == first
        assert repository.load_task_review_runtime("second") == second
        with sessions() as session:
            records = list(session.scalars(select(ArtifactOrm)))
            assert {row.task_id for row in records} == {"first", "second"}
            for row in records:
                assert repository._artifacts.read_text(row.uri) == '{"candidates": {}}'

        for task_id, uri, expected in (("first", references[0]["uri"], "UNIQUE"),
                                        ("missing", "file:///orphan", "FOREIGN KEY")):
            with pytest.raises(IntegrityError, match=expected):
                with sessions.begin() as session:
                    session.add(ArtifactOrm(id="invalid", task_id=task_id, kind="test", uri=uri,
                        sha256="invalid", size_bytes=0, retention_until=utcnow() + timedelta(days=1)))
        # 降级不对已合法重复的内容恢复旧约束，更不能合并或删除任务产物。
        migrate(path, "20260804_0002", direction="downgrade")
        assert repository.load_task_review_runtime("first") == first
        assert repository.load_task_review_runtime("second") == second
        migrate(path, "head")
        with sessions() as session:
            assert len(list(session.scalars(select(ArtifactOrm)))) == 2
    finally:
        engine.dispose()


def test_migration_refuses_unexpected_named_constraint(tmp_path):
    path = tmp_path / "unexpected.db"
    migrate(path, "20260804_0002")
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with engine.begin() as connection:
            operations = Operations(MigrationContext.configure(connection))
            with operations.batch_alter_table("artifacts") as batch:
                batch.create_unique_constraint("uq_artifacts_hash_kind", ["task_id", "kind"])
        with pytest.raises(subprocess.CalledProcessError) as caught:
            migrate(path, "head")
        assert "unexpected uq_artifacts_hash_kind columns" in caught.value.stderr
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "20260804_0002"
        assert any(item["name"] == "uq_artifacts_hash_kind"
                   for item in inspect(engine).get_unique_constraints("artifacts"))
    finally:
        engine.dispose()
