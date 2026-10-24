from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.api import reviews
from app.main import app
from app.models.review import PullRequestInfo
from app.services.preview.quick_preview import quick_preview, QuickPreviewStale
from app.tools.github.github_tool import GitHubTool


URL = "https://github.com/o/r/pull/1"


def github():
    pr = PullRequestInfo(owner="o", repo="r", number=1, title="概览", html_url=URL, clone_url="https://github.com/o/r.git",
        base={"sha": "a" * 40, "ref": "main", "repo_clone_url": "https://github.com/o/r.git"},
        head={"sha": "b" * 40, "ref": "feature", "repo_clone_url": "https://github.com/o/r.git"})
    return SimpleNamespace(fetch_pr=AsyncMock(return_value=pr), fetch_file_overview=AsyncMock(return_value=([
        {"filename": "new.py", "previous_filename": "old.py", "status": "renamed", "additions": 3,
         "deletions": 1, "patch": "private patch must not be returned"}], True)))


async def test_quick_api_uses_only_github_without_plan_or_job_credentials(monkeypatch):
    tool = github()
    # 没有仓库、Git、模型、锁或作业方法，防止快速路径隐式依赖完整准备。
    monkeypatch.setattr(reviews, "get_review_service", lambda: SimpleNamespace(github_tool=tool))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post("/api/reviews/preview/quick", json={"pr_url": URL})
        assert result.status_code == 200
        payload = result.json()
        assert payload["files"] == [{"path": "new.py", "previous_path": "old.py", "status": "renamed", "additions": 3, "deletions": 1}]
        assert payload["files_complete"] and payload["requires_full_preview"]
        assert "preview_id" not in payload and "plan_hash" not in payload and "patch" not in result.text
        assert (await client.post("/api/reviews/preview/quick", json={"pr_url": "https://evil.example/o/r/pull/1"})).status_code == 422
        assert (await client.post("/api/reviews/preview/quick", json={"pr_url": URL, "plan": {}})).status_code == 422
    assert tool.fetch_pr.await_count == 2 and tool.fetch_file_overview.await_count == 1


@pytest.mark.parametrize("side", ["base", "head"])
async def test_snapshot_moving_during_read_is_not_presented_as_consistent(side):
    tool = github()
    before = tool.fetch_pr.return_value
    after = before.model_copy(deep=True)
    getattr(after, side).sha = "c" * 40
    tool.fetch_pr.side_effect = [before, after]
    with pytest.raises(QuickPreviewStale):
        await quick_preview(tool, URL)


@pytest.mark.parametrize("failure,status", [(RuntimeError("token=secret /private/repo"), 502), (TimeoutError("secret"), 504),
    (QuickPreviewStale("secret"), 409)])
async def test_api_failure_is_sanitized_and_has_no_full_preview_fallback(monkeypatch, failure, status):
    tool = github()
    tool.fetch_pr.side_effect = failure
    monkeypatch.setattr(reviews, "get_review_service", lambda: SimpleNamespace(github_tool=tool))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post("/api/reviews/preview/quick", json={"pr_url": URL})
    assert result.status_code == status and "secret" not in result.text


@pytest.mark.parametrize("count,next_page,complete", [(0, False, True), (1, False, True), (1, True, False), (100, False, False)])
async def test_file_overview_is_bounded_to_one_page(monkeypatch, count, next_page, complete):
    original = httpx.AsyncClient
    requests = []
    def transport(request):
        requests.append(request)
        assert request.url.params["per_page"] == "100" and request.url.params["page"] == "1"
        headers = {"Link": '<https://api.github.com/next>; rel="next"'} if next_page else {}
        return httpx.Response(200, json=[{"filename": "a.py"}] * count, headers=headers)
    monkeypatch.setattr("app.tools.github.github_tool.httpx.AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(transport), **kwargs))
    files, actual = await GitHubTool().fetch_file_overview(URL)
    assert len(files) == count and actual is complete and len(requests) == 1


@pytest.mark.parametrize("metadata", [{"filename": "x.py", "status": "modified", "additions": -1, "deletions": 0},
    {"filename": "x.py", "status": "modified", "additions": True, "deletions": 0}])
async def test_untrusted_file_statistics_must_pass_schema_validation(monkeypatch, metadata):
    tool = github()
    tool.fetch_file_overview.return_value = ([metadata], True)
    monkeypatch.setattr(reviews, "get_review_service", lambda: SimpleNamespace(github_tool=tool))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post("/api/reviews/preview/quick", json={"pr_url": URL})
    assert result.status_code == 502
