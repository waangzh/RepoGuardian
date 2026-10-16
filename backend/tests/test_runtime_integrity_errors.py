import asyncio
import sqlite3
import traceback
from dataclasses import asdict

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models.review import ReviewTask
from app.services.coordination_runtime import CoordinationLeaseLost
from app.services.review_repository import _runtime_integrity_error
from test_phase6a_persistence import persistence as persistence


def runtime_payload(task_id, reference, revision=0):
    return {"version": "task-review-runtime-v1", "revision": revision,
            "spec": {"run_id": task_id}, "goals": {}, "pages": {}, "calls": {},
            "sessions": {"initial": reference}}


def test_artifact_constraint_error_is_not_cancellation_and_is_redacted(persistence):
    repository, _, sessions = persistence
    for task_id in ("first", "second"):
        repository.create_task(ReviewTask(id=task_id, pr_url="https://github.com/o/r/pull/1",
                                         engine="task_review_v1"))
    with sessions.begin() as session:
        session.execute(text("CREATE UNIQUE INDEX legacy_content ON artifacts (sha256, kind)"))
    secret = "private-model-output-token-must-not-leak"
    for task_id in ("first", "second"):
        artifact = repository._artifacts.put_text(task_id=task_id, kind="session", content=secret)
        payload = runtime_payload(task_id, asdict(artifact))
        if task_id == "first":
            repository.save_task_review_runtime(task_id, payload, -1)
            continue
        with pytest.raises(IntegrityError) as caught:
            repository.save_task_review_runtime(task_id, payload, -1)
        error = caught.value
        assert not isinstance(error, asyncio.CancelledError)
        rendered = "".join(traceback.format_exception(error))
        assert "SQLITE_CONSTRAINT_UNIQUE" in rendered and "task runtime" in rendered
        assert secret not in rendered and artifact.uri not in rendered
        assert artifact.sha256 not in rendered and "INSERT INTO" not in rendered
        assert error.params is None and error.statement is None
        assert repository.load_task_review_runtime(task_id) is None


@pytest.mark.parametrize("table,column,definition,expected", [
    ("side_effects", "idempotency_key", "TEXT PRIMARY KEY", CoordinationLeaseLost),
    ("artifacts", "id", "TEXT PRIMARY KEY", IntegrityError),
    ("artifacts", "uri", "TEXT UNIQUE", IntegrityError),
])
def test_only_side_effect_primary_key_collision_is_lease_loss(table, column, definition, expected):
    with sqlite3.connect(":memory:") as connection:
        connection.execute(f"CREATE TABLE {table} ({column} {definition})")
        connection.execute(f"INSERT INTO {table} VALUES (?)", ("secret",))
        with pytest.raises(sqlite3.IntegrityError) as caught:
            connection.execute(f"INSERT INTO {table} VALUES (?)", ("secret",))
    original = IntegrityError("INSERT secret SQL", {"secret": "value"}, caught.value)
    translated = _runtime_integrity_error(original, "run budget")
    assert isinstance(translated, expected)
    assert "secret" not in str(translated)


def test_unknown_integrity_error_remains_integrity_error_without_driver_text():
    original = IntegrityError("private SQL", {"token": "private"}, RuntimeError("private driver"))
    translated = _runtime_integrity_error(original, "task runtime")
    assert isinstance(translated, IntegrityError)
    assert "INTEGRITY_ERROR" in str(translated)
    assert "private" not in str(translated)
