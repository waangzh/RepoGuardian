from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import reviews
from app.models.orm import (ArtifactOrm, GitHubSummarySlotOrm, PatchOrm, PublicationAttemptOrm,
                            ReviewTaskOrm, ValidationOrm, WorkerJobOrm)
from app.models.review import PatchProposal, ReviewUnitResult, TaskStatus
from app.services.publication.publication_repository import PublicationConflict
from app.services.publication.publication_service import PublicationService
from app.services.review.review_repository import ReviewRepository
from app.services.review.review_service import ReviewService
from tests.support.persistence import _task, _unit
from test_publication_persistence import setup_publication


def completed(repository, status=TaskStatus.completed):
    task = _task()
    task.status = status
    task.report_markdown = "# saved report"
    task.review_units = [_unit()]
    task.patches = [PatchProposal(issue_ids=["issue"], title="fix", rationale="fix",
        unified_diff="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-a\n+b\n",
        touched_files=["app.py"], risk="low", head_sha="a" * 40)]
    repository.create_task(task)
    repository.save_task(task)
    return task


@pytest.mark.parametrize("status", [TaskStatus.completed, TaskStatus.completed_with_warnings,
                                   TaskStatus.failed, TaskStatus.cancelled])
def test_manual_delete_is_persistent_idempotent_and_hides_children(persistence, status):
    repository, _, sessions = persistence
    task = completed(repository, status)
    with sessions.begin() as session:
        patch = session.scalar(select(PatchOrm).where(PatchOrm.task_id == task.id))
        session.add(ValidationOrm(task_id=task.id, patch_id=patch.id, validation_id="validation",
            backend="user_runner", environment_fingerprint="fixture", patch_hash="fixture",
            validation_profile="fixture", fingerprint="fixture", status="passed"))
    assert repository.get_validation(task.id, "validation") is not None
    assert repository.get_unit(task.id, task.review_units[0].id) is not None
    assert repository.get_patch(task.id, task.patches[0].id) is not None
    assert repository.delete_task(task.id)
    assert repository.delete_task(task.id)
    restarted = ReviewRepository(sessions, repository._artifacts, require_migration=False)
    assert restarted.get_task(task.id) is None
    assert restarted.list_tasks().total == 0
    assert restarted.get_unit(task.id, task.review_units[0].id) is None
    assert restarted.get_patch(task.id, task.patches[0].id) is None
    assert restarted.get_validation(task.id, "validation") is None
    assert not restarted.delete_task("missing")
    with sessions() as session:
        row = session.get(ReviewTaskOrm, task.id)
        assert row.deleted_at is not None and row.report_markdown is None
        assert row.task_snapshot == {"id": task.id, "deleted": True}
        assert all(item.deleted_at is not None for item in session.scalars(
            select(ArtifactOrm).where(ArtifactOrm.task_id == task.id)))


@pytest.mark.parametrize("status", [TaskStatus.queued, TaskStatus.reviewing, TaskStatus.validating])
def test_running_reviews_cannot_be_deleted(persistence, status):
    repository, _, _ = persistence
    task = _task()
    task.status = status
    repository.create_task(task)
    with pytest.raises(ValueError, match="terminal"):
        repository.delete_task(task.id)
    assert repository.get_task(task.id) is not None


@pytest.mark.parametrize("status", ["queued", "retry", "leased"])
def test_active_jobs_block_deletion(persistence, status):
    repository, queue, sessions = persistence
    task = completed(repository)
    job_id = queue.enqueue(task_id=task.id)
    with sessions.begin() as session:
        session.get(WorkerJobOrm, job_id).status = status
    with pytest.raises(ValueError, match="review_job_active"):
        repository.delete_task(task.id)
    assert repository.get_task(task.id) is not None


@pytest.mark.parametrize("state", ["queued", "preparing", "publishing", "uncertain"])
def test_pending_publications_keep_audit_data(persistence, state):
    repository, _, sessions = persistence
    publications, plan, selection, context, _ = setup_publication(persistence)
    attempt = publications.create(plan, selection, context, "delete-test")
    with sessions.begin() as session:
        session.get(PublicationAttemptOrm, attempt.id).state = state
    with pytest.raises(ValueError, match="review_publication_pending"):
        repository.delete_task(plan.review_id)
    assert publications.frozen(attempt.id)[0]["review_id"] == plan.review_id


def test_deleted_review_rejects_late_writes_and_new_or_retried_jobs(persistence):
    repository, queue, sessions = persistence
    publications, plan, selection, context, _ = setup_publication(persistence)
    task = repository.get_task(plan.review_id)
    attempt = publications.create(plan, selection, context, "completed-publication")
    with sessions.begin() as session:
        session.get(PublicationAttemptOrm, attempt.id).state = "failed"
        job = session.scalar(select(WorkerJobOrm).where(WorkerJobOrm.task_id == task.id))
        job.status = "completed"
    assert repository.delete_task(task.id)
    task.status = TaskStatus.queued
    repository.save_task(task)
    assert repository.get_task(task.id) is None
    with pytest.raises(KeyError):
        queue.enqueue(task_id=task.id)
    with pytest.raises(ValueError, match="deleted"):
        repository.record_unit_result(task_id=task.id, unit=_unit(),
            result=ReviewUnitResult(review_unit_id="ru-test", status="completed", terminal_reason="no_issue"))
    with pytest.raises(KeyError):
        repository.save_finding_disposition(task.id, "finding", {})
    with pytest.raises(PublicationConflict, match="review_unavailable"):
        publications.create(plan, selection, context, "new-key")
    with pytest.raises(PublicationConflict, match="review_unavailable"):
        publications.retry(attempt.id)
    assert publications.get(attempt.id) is not None


def test_unreleased_summary_slot_blocks_delete_even_after_failed_attempt(persistence):
    repository, _, sessions = persistence
    publications, plan, selection, context, _ = setup_publication(persistence)
    attempt = publications.create(plan, selection, context, "pending-slot")
    lease = publications.acquire(attempt, "worker")
    publications.release(lease)
    with sessions.begin() as session:
        session.get(PublicationAttemptOrm, attempt.id).state = "failed"
        session.get(GitHubSummarySlotOrm, lease.slot_id).pending_publication_id = attempt.id
        session.scalar(select(WorkerJobOrm).where(WorkerJobOrm.task_id == plan.review_id)).status = "completed"
    with pytest.raises(ValueError, match="review_publication_pending"):
        repository.delete_task(plan.review_id)


@pytest.mark.asyncio
async def test_public_publication_access_requires_live_review(persistence):
    repository, _, _ = persistence
    task = completed(repository)
    repository.delete_task(task.id)
    service = PublicationService.__new__(PublicationService)
    service.task_loader = repository.get_task
    for operation in [service.get(task.id, "publication"), service.retry(task.id, "publication"),
                      service.history(task.id)]:
        with pytest.raises(KeyError, match="review_not_found"):
            await operation


@pytest.mark.parametrize("operation", ["enqueue", "publish"])
def test_delete_and_job_creation_are_serialized(persistence, operation):
    repository, queue, _ = persistence
    publications, plan, selection, context, _ = setup_publication(persistence)
    barrier = Barrier(2)

    def delete():
        barrier.wait()
        try:
            return repository.delete_task(plan.review_id)
        except ValueError:
            return False

    def create():
        barrier.wait()
        try:
            if operation == "publish":
                publications.create(plan, selection, context, "race")
            else:
                queue.enqueue(task_id=plan.review_id)
            return True
        except (KeyError, PublicationConflict):
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        deleted, created = executor.submit(delete), executor.submit(create)
        assert deleted.result() != created.result()


def test_delete_http_contract_and_related_endpoints(persistence, monkeypatch):
    repository, _, _ = persistence
    task = completed(repository)
    service = ReviewService.__new__(ReviewService)
    service._repository, service._tasks, service._run_tasks, service._dispositions = repository, {}, {}, {}
    monkeypatch.setattr(reviews, "get_review_service", lambda: service)
    app = FastAPI()
    app.include_router(reviews.router, prefix="/api")
    with TestClient(app) as client:
        path = "/api/reviews/" + task.id
        stream = client.get(path + "/stream")
        assert stream.status_code == 200 and "event: done" in stream.text
        assert client.delete(path).status_code == 204
        assert client.delete(path).content == b""
        for suffix in ["", "/units/ru-test", "/issues/issue", "/patches/" + task.patches[0].id,
                       "/validations/validation", "/publication-plan", "/publication-summary", "/discussions",
                       "/report", "/stream", "/files/0/diff", "/files/0/context"]:
            assert client.get(path + suffix).status_code == 404, suffix
        assert client.delete("/api/reviews/missing").status_code == 404
        running = _task()
        repository.create_task(running)
        result = client.delete("/api/reviews/" + running.id)
        assert result.status_code == 409 and result.json()["detail"]["code"] == "review_not_terminal"
