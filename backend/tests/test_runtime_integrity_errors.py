import asyncio
import sqlite3
import traceback
from dataclasses import asdict
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.orm import SideEffectOrm, WorkerJobOrm, utcnow
from app.models.review import ReviewTask, TaskStatus
from app.services.review.coordination_runtime import CoordinationLeaseLost
from app.services.review.review_repository import _runtime_integrity_error
from tests.support.persistence import persistence as persistence


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


@pytest.mark.parametrize("operation", ["runtime", "budget", "coordination"])
def test_repository_insert_race_is_lease_loss_and_preserves_committed_record(
    persistence, monkeypatch, operation,
):
    repository, _, sessions = persistence
    repository.create_task(ReviewTask(id="run", pr_url="https://github.com/o/r/pull/1",
                                     engine="task_review_v1"))
    if operation == "runtime":
        payload = runtime_payload("run", None)
        payload["sessions"] = {}
        key = "task-review-v1:run"
        def save():
            repository.save_task_review_runtime("run", payload, -1)
    elif operation == "budget":
        payload = {"revision": 0}
        key = "run-budget:run"
        def save():
            repository.save_run_budget("run", payload, -1)
    else:
        payload = {"revision": 0, "status": "active", "budget": {}}
        key = "cross-unit:run:fingerprint"
        def save():
            repository.save_coordination_runtime("run", "fingerprint", payload, -1, None)
    save()
    original = Session.get
    def miss_snapshot(session, entity, identity, *args, **kwargs):
        # 模拟 SELECT 未看到竞争写者，而 INSERT 时该主键已提交的边界。
        if entity is SideEffectOrm and identity == key:
            return None
        return original(session, entity, identity, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Session, "get", miss_snapshot)
        with pytest.raises(CoordinationLeaseLost, match="concurrent writer"):
            save()
    with sessions() as session:
        assert session.get(SideEffectOrm, key).result == payload


@pytest.mark.parametrize("rejection", ["cancelled", "expired", "wrong-owner", "stale-revision"])
def test_runtime_rejects_cancelled_expired_or_stale_writer(persistence, rejection):
    repository, _, sessions = persistence
    repository.create_task(ReviewTask(id="run", pr_url="https://github.com/o/r/pull/1",
                                     engine="task_review_v1"))
    payload = runtime_payload("run", None)
    payload["sessions"] = {}
    repository.save_task_review_runtime("run", payload, -1)
    before = deepcopy(repository.load_task_review_runtime("run"))
    if rejection == "cancelled":
        repository.cancel_task("run")
        before = repository.load_task_review_runtime("run")
        assert repository.get_task("run").status == TaskStatus.cancelled
    lease = None
    if rejection in {"expired", "wrong-owner"}:
        with sessions.begin() as session:
            session.add(WorkerJobOrm(id="job", task_id="run", kind="review", status="leased",
                idempotency_key="job-key", lease_owner="worker", attempts=1,
                lease_until=utcnow() + timedelta(seconds=-1 if rejection == "expired" else 30)))
        lease = {"job_id": "job", "owner": "other" if rejection == "wrong-owner" else "worker",
                 "attempt": 1}
    outgoing = {**payload, "revision": 1}
    if rejection == "stale-revision":
        outgoing = payload
    with pytest.raises(CoordinationLeaseLost):
        repository.save_task_review_runtime("run", outgoing, outgoing["revision"] - 1, lease)
    assert repository.load_task_review_runtime("run") == before
