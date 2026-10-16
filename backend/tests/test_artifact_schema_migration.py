import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.core import database


@pytest.mark.parametrize("revision,expected", [(None, False), ("20260804_0002", False),
                                              ("20261016_0003", True)])
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


def migrate(path, target):
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", target],
        cwd=Path(__file__).parents[1],
        env={**os.environ, "REPOGUARDIAN_DB_PATH": str(path)},
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
