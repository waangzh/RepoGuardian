"""真实队列重试期间不得向 API 发布误导性的失败终态。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.models.orm import WorkerJobOrm, utcnow
from app.models.review import ReviewCreateRequest, TaskStatus
from app.services.review.task_queue import ReviewWorker
from tests.support.review_plans import plan_service as plan_service
from tests.support.runtime_harness import production_input as production_input
from tests.support.persistence import persistence as persistence


@pytest.mark.parametrize("recover", [False, True])
async def test_intermediate_failure_remains_active_and_empty_errors_are_diagnostic(plan_service, monkeypatch, recover):
    service, *_ = plan_service
    task = service.create_task(ReviewCreateRequest(pr_url="https://github.com/o/r/pull/1"), dispatch=False)
    compiled = SimpleNamespace(aget_state=AsyncMock(return_value=SimpleNamespace(values={}, next=())),
                               ainvoke=AsyncMock(side_effect=httpx.ReadTimeout("")))
    monkeypatch.setattr("app.services.review.review_service.get_checkpointer", AsyncMock(return_value=None))
    monkeypatch.setattr("app.services.review.review_service.build_review_graph",
                        lambda: SimpleNamespace(compile=lambda **k: compiled))
    queue = service._task_queue
    worker = ReviewWorker(queue, service._handle_job, worker_id="test-retry")
    assert await worker.run_once()
    current = service.get_task(task.id)
    assert current.status == TaskStatus.queued and current.review.status == TaskStatus.queued
    assert current.error == "ReadTimeout"
    assert "自动重试" in current.steps[-1].message
    with queue._session_factory.begin() as session:
        job = session.query(WorkerJobOrm).filter_by(task_id=task.id).one()
        assert job.status == "retry" and job.last_error == "ReadTimeout"
        job.available_at = utcnow()
    if recover:
        compiled.ainvoke = AsyncMock(return_value={})
        def complete(task, result):
            task.status = task.review.status = TaskStatus.completed
            task.review.completed = True
            task.error = None
        monkeypatch.setattr(service, "_sync_result_to_task", complete)
        assert await worker.run_once()
        assert service.get_task(task.id).status == TaskStatus.completed
    else:
        for attempt in (2, 3):
            assert await worker.run_once()
            current = service.get_task(task.id)
            assert current.status == (TaskStatus.queued if attempt < 3 else TaskStatus.failed)
            with queue._session_factory.begin() as session:
                job = session.query(WorkerJobOrm).filter_by(task_id=task.id).one()
                assert job.status == ("retry" if attempt < 3 else "dead_letter")
                job.available_at = utcnow()


def test_stale_retry_writer_cannot_replace_current_attempt(plan_service):
    service, *_ = plan_service
    task = service.create_task(ReviewCreateRequest(pr_url="https://github.com/o/r/pull/1"), dispatch=False)
    job = service._task_queue.claim(worker_id="new-worker")
    task.status = TaskStatus.failed
    service._repository.save_task(task, lease={"job_id": job.id, "owner": "old-worker", "attempt": 1})
    assert service.get_task(task.id).status == TaskStatus.queued
    service._repository.save_task(task, lease={"job_id": job.id, "owner": job.lease_owner, "attempt": 0})
    assert service.get_task(task.id).status == TaskStatus.queued
