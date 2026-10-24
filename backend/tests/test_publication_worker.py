import httpx
import pytest

from app.services.publication.publication_worker import PublicationDelivery, PublicationWorker
from app.services.review.task_queue import ReviewWorker
from app.tools.github.github_publisher import GitHubPublisher
from app.services.publication.publication_preflight import PublicationPreflight
from test_sticky_summary import FakeGitHub
from tests.support.publication_fixtures import publish_config
from test_publication_persistence import setup_publication
from tests.support.persistence import persistence as persistence


def worker_setup(persistence):
    repo, plan, selection, context, queue = setup_publication(persistence)
    attempt = repo.create(plan, selection, context, "key")
    fake = FakeGitHub()
    github = GitHubPublisher("fixture", transport=httpx.MockTransport(fake))
    delivery = PublicationDelivery(repo, github, PublicationPreflight(github, publish_config()), persistence[0].get_task)
    return repo, attempt, fake, PublicationWorker(queue, delivery), delivery, queue


async def test_summary_inline_final_status_and_repeat_do_not_duplicate(persistence):
    repo, attempt, fake, worker, delivery, queue = worker_setup(persistence)
    assert await worker.run_once()
    result = repo.get(attempt.id)
    assert result.state == "succeeded", result
    assert all(item.state == "published" and item.github_url for item in result.items)
    assert len(fake.comments) == len(fake.reviews) == len(fake.inline) == 1
    assert "Inline：1" in next(iter(fake.comments.values()))["body"]
    body = next(iter(fake.comments.values()))["body"]
    assert f"[查看 Inline 讨论]({next(iter(fake.inline.values()))['html_url']})" in body
    assert not await worker.run_once()
    assert not any(value[0] == "APPROVE" for value in fake.writes)


async def test_timeout_after_inline_acceptance_recovers_batch_without_post_replay(persistence):
    repo, attempt, fake, worker, delivery, queue = worker_setup(persistence)
    original = fake.__class__.__call__
    def transport(request):
        if request.method == "POST" and request.url.path.endswith("/reviews"):
            fake.timeout = True
        return original(fake, request)
    delivery.github._transport = httpx.MockTransport(transport)
    await worker.run_once()
    assert repo.get(attempt.id).state == "uncertain" and len(fake.reviews) == 1
    delivery.github._transport = httpx.MockTransport(fake)
    repo.retry(attempt.id)
    await worker.run_once()
    assert repo.get(attempt.id).state == "succeeded"
    assert len(fake.reviews) == len(fake.inline) == 1
    assert "[查看 Inline 讨论](https://github.com/o/r/pull/1#discussion_r1000)" in next(iter(fake.comments.values()))["body"]


async def test_absent_remote_after_unknown_send_requires_manual_handling(persistence):
    repo, attempt, fake, worker, delivery, queue = worker_setup(persistence)
    original = fake.__class__.__call__
    def transport(request):
        if request.method == "POST" and request.url.path.endswith("/reviews"):
            raise httpx.ReadTimeout("not observable")
        return original(fake, request)
    delivery.github._transport = httpx.MockTransport(transport)
    await worker.run_once()
    assert repo.get(attempt.id).state == "uncertain"
    delivery.github._transport = httpx.MockTransport(fake)
    repo.retry(attempt.id)
    await worker.run_once()
    assert repo.get(attempt.id).state == "uncertain" and not fake.reviews
    assert repo.lookup_slot(123, 1, "user:7").pending_publication_id == attempt.id


async def test_review_worker_and_cancel_do_not_consume_publication(persistence):
    repo, attempt, fake, worker, delivery, queue = worker_setup(persistence)
    async def unexpected(job):
        pytest.fail("review worker consumed a publication")
    assert not await ReviewWorker(queue, unexpected).run_once()
    assert queue.cancel_for_task(attempt.review_id) == 0
    assert await worker.run_once() and repo.get(attempt.id).state == "succeeded"


async def test_new_head_blocks_every_write(persistence):
    repo, attempt, fake, worker, delivery, queue = worker_setup(persistence)
    fake.head = "d" * 40
    await worker.run_once()
    assert repo.get(attempt.id).state == "stale" and not fake.writes


async def test_same_head_new_review_reuses_inline_and_old_review_cannot_replace_summary(persistence):
    from datetime import timedelta
    from app.services.publication.publication_planner import PublicationPlanner
    from app.models.publication import PublicationSelection
    from app.core.fingerprints import stable_hash
    repo, attempt, fake, worker, delivery, queue = worker_setup(persistence)
    await worker.run_once()
    task = persistence[0].get_task(attempt.review_id).model_copy(deep=True)
    task.id = "run-new"
    task.run_manifest.review_id = task.id
    task.run_manifest.completed_at += timedelta(days=1)
    persistence[0].create_task(task)
    checked = await delivery.preflight.check(task)
    selection = PublicationSelection(expected_plan_hash=checked["plan_hash"], expected_head_sha=attempt.head_sha, finding_keys=checked["finding_keys"])
    new_attempt = repo.create(PublicationPlanner().build(task), selection, checked["context"], "new")
    await worker.run_once()
    assert repo.get(new_attempt.id).state == "succeeded"
    assert len(fake.comments) == len(fake.reviews) == len(fake.inline) == 1
    plan, _, context = repo.frozen(attempt.id)
    selection = PublicationSelection(expected_plan_hash=stable_hash(plan), expected_head_sha=attempt.head_sha,
                                     finding_keys=[], acknowledge_partial_review=True)
    old = repo.create(PublicationPlanner().build(persistence[0].get_task(attempt.review_id)), selection, context, "late-old")
    writes = len(fake.writes)
    await worker.run_once()
    assert repo.get(old.id).state == "stale" and len(fake.writes) == writes


async def test_reconcile_summary_before_stale_check(persistence):
    repo, attempt, fake, worker, delivery, queue = worker_setup(persistence)
    fake.timeout = True
    await worker.run_once()
    assert repo.get(attempt.id).state == "uncertain" and len(fake.comments) == 1
    fake.head = "d" * 40
    repo.retry(attempt.id)
    await worker.run_once()
    assert repo.get(attempt.id).state == "stale"
    assert repo.lookup_slot(123, 1, "user:7").pending_publication_id is None
    assert len(fake.writes) == 1


async def test_rate_limit_is_deferred_without_writing(persistence):
    from datetime import timedelta
    from sqlalchemy import select
    from app.models.orm import WorkerJobOrm, utcnow
    repo, attempt, fake, worker, delivery, queue = worker_setup(persistence)
    delivery.github._transport = httpx.MockTransport(lambda request: httpx.Response(429, headers={"Retry-After": "120"}, json={}))
    before = utcnow().replace(tzinfo=None)
    await worker.run_once()
    assert not fake.writes and repo.get(attempt.id).state == "failed"
    with repo.sessions() as session:
        job = session.scalar(select(WorkerJobOrm).where(WorkerJobOrm.kind == "github_publication"))
        assert job.status == "retry" and job.available_at >= before + timedelta(seconds=120)


async def test_expired_worker_recovers_committed_summary_without_reposting(persistence):
    from datetime import timedelta
    from app.models.orm import WorkerJobOrm, GitHubSummarySlotOrm, utcnow
    from app.tools.github.github_publisher import GitHubPublishError
    repo, attempt, fake, worker, delivery, queue = worker_setup(persistence)
    job = queue.claim(worker_id="crashed", kinds=("github_publication",))
    lease = repo.acquire(attempt, "crashed", job)
    fake.timeout = True
    with pytest.raises(GitHubPublishError):
        await delivery.sticky.upsert(attempt, lease, "发送成功但未拿到响应")
    with repo.sessions.begin() as session:
        session.get(WorkerJobOrm, job.id).lease_until = utcnow() - timedelta(seconds=1)
        session.get(GitHubSummarySlotOrm, lease.slot_id).lease_until = utcnow() - timedelta(seconds=1)
    await worker.run_once()
    assert repo.get(attempt.id).state == "succeeded"
    assert len(fake.comments) == len(fake.inline) == 1
    assert len([method for method, path in fake.writes if method == "POST" and path.endswith("/issues/1/comments")]) == 1


async def test_lost_job_generation_blocks_writes(persistence):
    from app.models.orm import WorkerJobOrm
    from app.services.publication.publication_repository import PublicationLeaseLost
    repo, attempt, fake, worker, delivery, queue = worker_setup(persistence)
    job = queue.claim(worker_id="old", kinds=("github_publication",))
    with repo.sessions.begin() as session:
        session.get(WorkerJobOrm, job.id).attempts += 1
    with pytest.raises(PublicationLeaseLost):
        await delivery.execute(job)
    assert not fake.writes


@pytest.mark.parametrize("state,fallback,selected,url,linked", [
    ("published", False, True, "https://github.com/o/r/pull/1#discussion_r1000", True),
    ("uncertain", False, True, "https://github.com/o/r/pull/1#discussion_r1000", False),
    ("failed", False, True, "https://github.com/o/r/pull/1#discussion_r1000", False),
    ("published", True, True, "https://github.com/o/r/pull/1#discussion_r1000", False),
    ("published", False, False, "https://github.com/o/r/pull/1#discussion_r1000", False),
    ("published", False, True, "javascript:alert(1)", False),
    ("published", False, True, "https://github.com/o/r/pull/1#discussion_r1000) @all", False),
    ("published", False, True, None, False),
])
def test_summary_links_only_confirmed_selected_inline_receipts(state, fallback, selected, url, linked):
    from types import SimpleNamespace
    from app.services.publication.publication_worker import compose_summary
    from app.services.publication.publication_planner import PublicationPlanner
    from tests.support.review_tasks import publishable_task as publishable_task
    plan = PublicationPlanner().build(publishable_task())
    saved = plan.model_dump(mode="json")
    item = SimpleNamespace(kind="inline", state=state, finding_key="key-a", github_url=url)
    result = compose_summary(plan, {"finding_keys": ["key-a"] if selected else []},
        {"key-a": "outside_diff"} if fallback else {}, [item], "Published")
    assert ("查看 Inline 讨论" in result) is linked
    assert plan.model_dump(mode="json") == saved
