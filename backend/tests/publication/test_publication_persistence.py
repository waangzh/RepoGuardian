from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select, func, inspect, create_engine

from app.models.orm import PublicationAttemptOrm, PublicationItemOrm, WorkerJobOrm, GitHubSummarySlotOrm
from app.models.publication import PublicationSelection
from app.services.publication.publication_planner import PublicationPlanner
from app.services.publication.publication_repository import PublicationRepository, PublicationConflict
from app.core.fingerprints import stable_hash
from tests.support.persistence import persistence as persistence
from tests.support.review_tasks import publishable_task as publishable_task
from tests.review.test_artifact_schema_migration import migrate


def setup_publication(persistence):
    review_repo, queue, sessions = persistence
    task = publishable_task()
    review_repo.create_task(task)
    plan = PublicationPlanner().build(task)
    selection = PublicationSelection(expected_plan_hash=stable_hash(plan.model_dump(mode="json")),
        expected_head_sha=plan.snapshot.head_sha, finding_keys=[plan.inline_drafts[0].finding_key])
    context = {"repository_id": 123, "pr_number": 1, "publisher_identity": "user:7", "owner": "o", "repo": "r",
               "base_sha": task.run_manifest.base_sha, "order": task.run_manifest.completed_at.isoformat() + ":" + task.id, "publisher_id": 7}
    return PublicationRepository(sessions), plan, selection, context, queue


def test_create_is_atomic_idempotent_and_concurrent(persistence):
    repo, plan, selection, context, _ = setup_publication(persistence)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda key: repo.create(plan, selection, context, key), ["key", "key", "other", "key"]))
    assert len({result.id for result in results}) == 1
    assert len(results[0].items) == 2
    assert repo.frozen(results[0].id)[1]["finding_keys"] == selection.finding_keys
    selection.finding_keys = []
    with pytest.raises(PublicationConflict, match="idempotency_key_conflict"):
        repo.create(plan, selection, context, "other")
    with repo.sessions() as session:
        assert session.scalar(select(func.count()).select_from(PublicationAttemptOrm)) == 1
        assert session.scalar(select(func.count()).select_from(WorkerJobOrm).where(WorkerJobOrm.kind == "github_publication")) == 1


def test_pr_lock_is_shared_across_attempts_and_fences_old_writer(persistence):
    repo, plan, selection, context, _ = setup_publication(persistence)
    attempt = repo.create(plan, selection, context, "key")
    lease = repo.acquire(attempt, "worker-a")
    with pytest.raises(PublicationConflict, match="busy"):
        repo.acquire(attempt, "worker-b")
    repo.release(lease)
    next_lease = repo.acquire(attempt, "worker-b")
    assert next_lease.revision > lease.revision
    with pytest.raises(PublicationConflict, match="lease_lost"):
        repo.set_state(attempt.id, "succeeded", lease=lease)
    assert repo.slot(next_lease).repository_id == 123
    repo.release(next_lease)


def test_queue_failure_rolls_back_attempt_items_and_request_key(persistence):
    from sqlalchemy import event
    repo, plan, selection, context, _ = setup_publication(persistence)
    def reject_job(session, *_):
        if any(isinstance(row, WorkerJobOrm) for row in session.new):
            raise RuntimeError("queue unavailable")
    event.listen(repo.sessions.class_, "before_flush", reject_job)
    try:
        with pytest.raises(RuntimeError, match="queue unavailable"):
            repo.create(plan, selection, context, "key")
    finally:
        event.remove(repo.sessions.class_, "before_flush", reject_job)
    assert not repo.list(plan.review_id)
    assert repo.existing_request(plan.review_id, selection, context["publisher_identity"], "key") is None


def test_retention_keeps_active_publications_and_summary_slot_survives_task_cleanup(persistence):
    from datetime import timedelta
    from app.models.orm import ReviewTaskOrm, utcnow
    repo, plan, selection, context, _ = setup_publication(persistence)
    attempt = repo.create(plan, selection, context, "key")
    with repo.sessions.begin() as session:
        session.get(ReviewTaskOrm, plan.review_id).retention_until = utcnow() - timedelta(days=1)
    assert not persistence[0].delete_task_if_retention_elapsed(plan.review_id)
    lease = repo.acquire(attempt, "worker")
    repo.set_state(attempt.id, "failed", lease=lease)
    repo.release(lease)
    assert persistence[0].delete_task_if_retention_elapsed(plan.review_id)
    assert repo.lookup_slot(123, 1, "user:7") is not None


def test_migration_upgrade_and_downgrade_preserve_review_and_slot_has_no_review_fk(tmp_path):
    path = tmp_path / "migration.db"
    migrate(path, "20261016_0003")
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    assert "github_publication_attempts" not in inspect(engine).get_table_names()
    migrate(path, "head")
    inspector = inspect(engine)
    assert {PublicationAttemptOrm.__tablename__, PublicationItemOrm.__tablename__, GitHubSummarySlotOrm.__tablename__} <= set(inspector.get_table_names())
    assert not inspector.get_foreign_keys("github_summary_slots")
    assert any(item["name"] == "uq_publication_item" for item in inspector.get_unique_constraints("github_publication_items"))
    migrate(path, "20261016_0003", direction="downgrade")
    assert "review_tasks" in inspect(engine).get_table_names()
    assert "github_publication_attempts" not in inspect(engine).get_table_names()
    engine.dispose()
