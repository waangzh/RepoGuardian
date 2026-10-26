import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app.core.fingerprints import stable_hash
from app.models.publication import PublicationSelection
from app.services.publication.publication_repository import PublicationRepository, PublicationConflict
from app.services.publication.publication_service import PublicationService
from app.services.review.task_queue import DatabaseTaskQueue
from app.tools.github.github_publisher import GitHubPublisher, GitHubPublishError
from tests.review.test_native_lifecycle import preview_and_create
from tests.support.publication_fixtures import publish_config

pytest_plugins = ["tests.review.test_native_lifecycle"]


class GitHubAPI:
    """Only mocked HTTP is used; no GitHub writes leave this process."""

    def __init__(self, task):
        self.task = task
        self.head = task.snapshot.head_sha
        self.base = task.snapshot.declared_base_sha
        self.permission = True
        self.comments, self.reviews, self.inline, self.writes = {}, {}, {}, []
        self.timeout_summary = False
        self.timeout_inline = False

    def __call__(self, request):
        path, method = request.url.path, request.method
        root = "/repos/local/repo"
        if method in {"POST", "PATCH"}:
            payload = json.loads(request.content)
            self.writes.append((method, path, payload))
            if path.endswith("/reviews"):
                identity = 100 + len(self.reviews)
                value = {"id": identity, "body": payload["body"], "commit_id": payload["commit_id"],
                    "state": "COMMENTED", "user": {"id": 7},
                    "pull_request_url": f"https://api.github.com{root}/pulls/1",
                    "html_url": f"https://github.com/local/repo/pull/1#pullrequestreview-{identity}"}
                self.reviews[identity] = value
                for comment in payload["comments"]:
                    cid = 1000 + len(self.inline)
                    self.inline[cid] = {**comment, "id": cid, "pull_request_review_id": identity,
                        "commit_id": payload["commit_id"], "user": {"id": 7},
                        "pull_request_url": value["pull_request_url"],
                        "html_url": f"https://github.com/local/repo/pull/1#discussion_r{cid}"}
                if self.timeout_inline:
                    self.timeout_inline = False
                    raise httpx.ReadTimeout("accepted before transport failure")
            else:
                identity = int(path.rsplit("/", 1)[-1]) if method == "PATCH" else 10 + len(self.comments)
                value = {"id": identity, "body": payload["body"], "user": {"id": 7},
                    "issue_url": f"https://api.github.com{root}/issues/1",
                    "html_url": f"https://github.com/local/repo/pull/1#issuecomment-{identity}"}
                self.comments[identity] = value
                if self.timeout_summary:
                    self.timeout_summary = False
                    raise httpx.ReadTimeout("accepted before transport failure")
            return httpx.Response(200, json=value)
        if path == "/user":
            value = {"id": 7}
        elif path == root:
            value = {"id": 123, "full_name": "local/repo", "permissions": {"push": self.permission}}
        elif path.endswith("/pulls/1"):
            value = {"number": 1, "state": "open", "merged": False, "changed_files": 1,
                     "head": {"sha": self.head}, "base": {"sha": self.base, "repo": {"id": 123}}}
        elif path.endswith("/files"):
            value = [{"filename": "safe.py", "status": "modified", "additions": 1, "deletions": 1,
                      "patch": "@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2"}]
        elif "/issues/comments/" in path:
            value = self.comments.get(int(path.rsplit("/", 1)[-1]))
            if value is None:
                return httpx.Response(404, json={})
        elif path.endswith("/issues/1/comments"):
            value = list(self.comments.values())
        elif "/reviews/" in path and path.endswith("/comments"):
            identity = int(path.split("/")[-2])
            value = [comment for comment in self.inline.values() if comment["pull_request_review_id"] == identity]
        elif path.endswith("/pulls/1/reviews"):
            value = list(self.reviews.values())
        elif path.endswith("/pulls/1/comments"):
            value = list(self.inline.values())
        else:
            raise AssertionError((method, path))
        return httpx.Response(200, json=value)


async def setup(pipeline):
    service, repository, _, _, _ = pipeline
    _, _, task = await preview_and_create(service)
    assert await service._worker.run_once()
    task = repository.get_task(task.id)
    api = GitHubAPI(task)
    publications = PublicationRepository(repository._session_factory)
    queue = DatabaseTaskQueue(repository._session_factory)
    delivery = PublicationService(publications,
        GitHubPublisher("fixture", transport=httpx.MockTransport(api)), publish_config(),
        repository.get_task, queue=queue, artifacts=repository._artifacts)
    return task, api, delivery, repository


async def create_attempt(task, delivery):
    preflight = await delivery.preflight(task.id)
    selection = PublicationSelection(expected_plan_hash=preflight.plan_hash,
        expected_head_sha=preflight.head_sha, finding_keys=preflight.finding_keys,
        acknowledge_partial_review=True)
    attempt = await delivery.create(task.id, selection, "request-key")
    return attempt, selection


async def test_delivery_rechecks_snapshot_and_idempotent_repeat(pipeline):
    task, api, delivery, repository = await setup(pipeline)
    attempt, selection = await create_attempt(task, delivery)
    with pytest.raises(ValueError, match="publication_pending"):
        repository.delete_task(task.id)
    assert await delivery.worker.run_once()
    result = delivery.repo.get(attempt.id)
    assert result.state == "succeeded", result
    assert len(api.comments) == len(api.reviews) == len(api.inline) == 1
    assert all(item.state == "published" for item in result.items)
    repeated = await delivery.create(task.id, selection, "request-key")
    assert repeated.id == result.id
    assert not await delivery.worker.run_once()
    assert len(api.inline) == 1
    assert all(payload.get("event", "COMMENT") == "COMMENT" for _, _, payload in api.writes)


@pytest.mark.parametrize("kind", ["summary", "inline"])
async def test_unknown_accepted_post_reconciles_without_duplicate(pipeline, kind):
    task, api, delivery, _ = await setup(pipeline)
    attempt, _ = await create_attempt(task, delivery)
    setattr(api, "timeout_" + kind, True)
    assert await delivery.worker.run_once()
    assert delivery.repo.get(attempt.id).state == "uncertain"
    delivery.repo.retry(attempt.id)
    assert await delivery.worker.run_once()
    result = delivery.repo.get(attempt.id)
    assert result.state == "succeeded", result
    assert len(api.comments) == len(api.reviews) == len(api.inline) == 1
    posts = [path for method, path, _ in api.writes if method == "POST"]
    assert posts.count("/repos/local/repo/issues/1/comments") == 1
    assert posts.count("/repos/local/repo/pulls/1/reviews") == 1


async def test_stale_head_and_permission_denied_never_write(pipeline):
    task, api, delivery, _ = await setup(pipeline)
    api.head = "0" * 40
    with pytest.raises(GitHubPublishError, match="publication_stale"):
        await delivery.preflight(task.id)
    api.head, api.permission = task.snapshot.head_sha, False
    with pytest.raises(GitHubPublishError, match="permission_denied"):
        await delivery.preflight(task.id)
    assert api.writes == []


async def test_head_moves_after_outbox_creation_never_publishes(pipeline):
    task, api, delivery, _ = await setup(pipeline)
    attempt, _ = await create_attempt(task, delivery)
    api.head = "0" * 40
    assert await delivery.worker.run_once()
    assert delivery.repo.get(attempt.id).state == "stale"
    assert api.writes == []


async def test_outbox_concurrent_requests_share_one_logical_delivery(pipeline):
    task, _, delivery, _ = await setup(pipeline)
    checked = await delivery.checked(task.id)
    plan = checked["plan"]
    selection = PublicationSelection(expected_plan_hash=stable_hash(plan.model_dump(mode="json")),
        expected_head_sha=plan.snapshot.head_sha, finding_keys=checked["finding_keys"])
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda key: delivery.repo.create(plan, selection, checked["context"], key).id,
                            ["one", "one", "two", "three"]))
    assert len(set(ids)) == 1
    selection.finding_keys = []
    with pytest.raises(PublicationConflict, match="idempotency_key_conflict"):
        delivery.repo.create(plan, selection, checked["context"], "one")
