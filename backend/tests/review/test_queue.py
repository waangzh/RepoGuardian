from datetime import timedelta

import pytest
from sqlalchemy import update

from app.models.orm import WorkerJobOrm
from app.review.domain import utcnow
from app.review.store import LeaseLost
from app.services.review.task_queue import DatabaseTaskQueue
from tests.review.vnext_helpers import setup_run


def test_same_worker_reclaim_does_not_authorize_old_attempt(tmp_path):
    store, run, lease, sessions = setup_run(tmp_path)
    queue = DatabaseTaskQueue(sessions)
    with sessions.begin() as session:
        session.execute(update(WorkerJobOrm).where(WorkerJobOrm.id == lease["job_id"]).values(
            lease_until=utcnow() - timedelta(seconds=1)))
    assert not queue.heartbeat(lease["job_id"], lease["owner"], attempt=lease["attempt"])
    replacement = queue.claim(worker_id=lease["owner"], kinds=("review",))
    assert replacement.attempts == lease["attempt"] + 1
    assert not queue.complete(lease["job_id"], lease["owner"], attempt=lease["attempt"])
    with pytest.raises(ValueError, match="lease"):
        queue.fail(lease["job_id"], lease["owner"], "old error", attempt=lease["attempt"])
    with pytest.raises(LeaseLost):
        store.assert_active(run.id, lease)
    assert queue.heartbeat(replacement.id, replacement.lease_owner, attempt=replacement.attempts)


def test_last_expired_attempt_finishes_facts_without_resetting_unknown_cost(tmp_path):
    store, run, lease, sessions = setup_run(tmp_path)
    work = store.claim(run.id, store.work_items(run.id)[0].id, lease)
    store.reserve(run.id, work, 5000, lease)
    with sessions.begin() as session:
        session.execute(update(WorkerJobOrm).where(WorkerJobOrm.id == lease["job_id"]).values(
            attempts=3, max_attempts=3, lease_until=utcnow() - timedelta(seconds=1)))
    assert DatabaseTaskQueue(sessions).claim(worker_id="replacement", kinds=("review",)) is None
    recovered = store.get(run.id)
    assert recovered.status == "failed"
    assert recovered.report.overview.error_code == "interrupted_unknown"
    assert recovered.report.usage["committed_input_tokens"] == 5000
    assert recovered.report.usage["input_tokens"] is None
