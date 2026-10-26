import json

import httpx
import pytest

from app.services.review.sticky_summary import StickySummary
from app.services.publication.publication_preflight import PublicationPreflight
from app.tools.github.github_publisher import GitHubPublisher, GitHubPublishError
from tests.support.publication_fixtures import github_fixture, publish_config
from tests.publication.test_publication_persistence import setup_publication
from tests.support.persistence import persistence as persistence


class FakeGitHub:
    def __init__(self):
        self.comments, self.reviews, self.inline, self.writes = {}, {}, {}, []
        self.timeout = False
        self.head = "a" * 40

    def __call__(self, request):
        path, method = request.url.path, request.method
        if method in {"POST", "PATCH"}:
            self.writes.append((method, path))
            payload = json.loads(request.content)
            if path.endswith("/reviews"):
                identity = len(self.reviews) + 100
                review = {"id": identity, "body": payload["body"], "commit_id": payload["commit_id"], "state": "COMMENTED",
                    "user": {"id": 7}, "pull_request_url": "https://api.github.com/repos/o/r/pulls/1",
                    "html_url": f"https://github.com/o/r/pull/1#pullrequestreview-{identity}"}
                self.reviews[identity] = review
                for comment in payload["comments"]:
                    cid = len(self.inline) + 1000
                    self.inline[cid] = {**comment, "id": cid, "pull_request_review_id": identity, "commit_id": payload["commit_id"],
                        "user": {"id": 7}, "pull_request_url": review["pull_request_url"],
                        "html_url": f"https://github.com/o/r/pull/1#discussion_r{cid}"}
                result = review
            else:
                identity = int(path.rsplit("/", 1)[-1]) if method == "PATCH" else len(self.comments) + 10
                result = {"id": identity, "body": payload["body"], "user": {"id": 7},
                    "issue_url": "https://api.github.com/repos/o/r/issues/1",
                    "html_url": f"https://github.com/o/r/pull/1#issuecomment-{identity}"}
                self.comments[identity] = result
            if self.timeout:
                self.timeout = False
                raise httpx.ReadTimeout("uncertain")
            return httpx.Response(200, json=result)
        if "/issues/comments/" in path:
            value = self.comments.get(int(path.rsplit("/", 1)[-1]))
            return httpx.Response(200 if value else 404, json=value or {})
        if path.endswith("/issues/1/comments"):
            return httpx.Response(200, json=list(self.comments.values()))
        if "/reviews/" in path and path.endswith("/comments"):
            identity = int(path.split("/")[-2])
            return httpx.Response(200, json=[value for value in self.inline.values() if value["pull_request_review_id"] == identity])
        if path.endswith("/pulls/1/reviews"):
            return httpx.Response(200, json=list(self.reviews.values()))
        if path.endswith("/pulls/1/comments"):
            return httpx.Response(200, json=list(self.inline.values()))
        return github_fixture(request, head=self.head)


def sticky_setup(persistence):
    repo, plan, selection, context, queue = setup_publication(persistence)
    attempt = repo.create(plan, selection, context, "key")
    fake = FakeGitHub()
    github = GitHubPublisher("fixture", transport=httpx.MockTransport(fake))
    sticky = StickySummary(repo, github, PublicationPreflight(github, publish_config()))
    return repo, attempt, fake, sticky, queue


async def test_sticky_upsert_and_recover_unknown_post_without_duplicate(persistence):
    repo, attempt, fake, sticky, _ = sticky_setup(persistence)
    lease = repo.acquire(attempt, "worker")
    fake.timeout = True
    with pytest.raises(GitHubPublishError) as caught:
        await sticky.upsert(attempt, lease, "初始摘要")
    assert caught.value.uncertain and len(fake.comments) == 1
    await sticky.upsert(attempt, lease, "初始摘要")
    assert len(fake.writes) == 1 and repo.get(attempt.id).items[-1].state == "published"
    await sticky.upsert(attempt, lease, "最终摘要")
    assert len(fake.comments) == 1 and fake.writes[-1][0] == "PATCH"
    repo.release(lease)


async def test_foreign_author_manual_edit_and_old_order_cannot_be_overwritten(persistence):
    repo, attempt, fake, sticky, _ = sticky_setup(persistence)
    lease = repo.acquire(attempt, "worker")
    await sticky.upsert(attempt, lease, "合法摘要")
    remote = next(iter(fake.comments.values()))
    remote["user"]["id"] = 8
    with pytest.raises(GitHubPublishError, match="identity_conflict"):
        await sticky.upsert(attempt, lease, "禁止覆盖")
    remote["user"]["id"] = 7
    remote["body"] = "被手工修改" + remote["body"]
    with pytest.raises(GitHubPublishError, match="identity_conflict"):
        await sticky.upsert(attempt, lease, "禁止覆盖")
    from app.models.orm import GitHubSummarySlotOrm
    with repo.locked(lease) as session:
        session.get(GitHubSummarySlotOrm, lease.slot_id).latest_order = "9999"
    with pytest.raises(GitHubPublishError, match="newer_review"):
        await sticky.upsert(attempt, lease, "旧结果")
    assert len(fake.writes) == 1
