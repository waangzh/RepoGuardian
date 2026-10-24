from types import SimpleNamespace

import httpx
import pytest

from app.api import reviews
from app.main import app
from app.tools.github.github_tool import GitHubToolError
from tests.support.review_tasks import publishable_task as publishable_task
from tests.support.persistence import persistence as persistence


@pytest.mark.parametrize("remote,expected", [("a" * 40, "current"), ("d" * 40, "stale"), ("invalid", "unknown"), (None, "unknown")])
async def test_copy_check_binds_snapshot_keeps_human_annotations_and_network_unknown(monkeypatch, remote, expected):
    task = publishable_task()
    original = task.model_dump(mode="json")
    saved = {"key-a": {"current": {"status": "resolved", "reason": "人工复核已处理"}}}
    service = SimpleNamespace(get_task=lambda identity: task, finding_dispositions=lambda task_id: saved)
    monkeypatch.setattr(reviews, "get_review_service", lambda: service)
    calls = []

    async def fetch(self, url):
        calls.append(url)
        if remote is None:
            raise GitHubToolError("github_connection_failed")
        return SimpleNamespace(head=SimpleNamespace(sha=remote))

    monkeypatch.setattr(reviews.GitHubTool, "fetch_pr", fetch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post(f"/api/reviews/{task.id}/review-comment-check")
        plan = (await client.get(f"/api/reviews/{task.id}/publication-plan")).json()
    assert result.status_code == 200
    body = result.json()
    assert body["status"] == expected and body["reviewed_head_sha"] == "a" * 40
    assert body["checked_at"] and body["review_id"] == task.id
    assert body["preview_markdown"] == plan["review_comment_markdown"]
    assert body["review_comment_markdown"].startswith(body["preview_markdown"])
    assert "快照检查" not in body["preview_markdown"]
    # 正常状态只在正文保留一条简短提交声明；过期或未知才追加检查说明。
    assert ("快照检查" in body["review_comment_markdown"]) is (expected != "current")
    assert body["preview_markdown"].count(f"`{'a' * 40}`") == 1
    assert "人工处理：已处理" in body["review_comment_markdown"]
    assert "人工处理：已处理" in plan["user_report_markdown"]
    assert calls == [task.pr_url]  # 普通计划读取不会增加远端访问。
    assert task.model_dump(mode="json") == original


@pytest.mark.parametrize("status,expected", [("reviewing", 409), (None, 404)])
async def test_copy_check_rejects_missing_and_unfinished_before_network(monkeypatch, status, expected):
    task = publishable_task()
    if status:
        task.status = status
    monkeypatch.setattr(reviews, "get_review_service", lambda: SimpleNamespace(
        get_task=lambda identity: task if status else None, finding_dispositions=lambda _: {}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/api/reviews/run/review-comment-check")).status_code == expected


async def test_missing_snapshot_never_fetches_or_claims_current(monkeypatch):
    task = publishable_task()
    task.run_manifest = None
    monkeypatch.setattr(reviews, "get_review_service", lambda: SimpleNamespace(
        get_task=lambda identity: task, finding_dispositions=lambda _: {}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        body = (await client.post("/api/reviews/run/review-comment-check")).json()
    assert body["status"] == "unknown" and body["latest_head_sha"] is None
    assert "未能确认远端 Head" in body["review_comment_markdown"]


async def test_persisted_disposition_refreshes_delivery_without_rewriting_frozen_report(persistence, monkeypatch):
    repository, _, _ = persistence
    task = publishable_task()
    task.report_markdown = "已冻结的原始技术记录"
    repository.create_task(task)
    service = SimpleNamespace(get_task=repository.get_task,
        finding_dispositions=repository.finding_dispositions,
        save_finding_disposition=lambda task_id, finding_id, value: repository.save_finding_disposition(
            task_id, finding_id, {"current": value}))
    monkeypatch.setattr(reviews, "get_review_service", lambda: service)

    async def fetch(self, url):
        return SimpleNamespace(head=SimpleNamespace(sha="a" * 40))

    monkeypatch.setattr(reviews.GitHubTool, "fetch_pr", fetch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        original = (await client.get("/api/reviews/run/publication-plan")).json()
        preview = (await client.post("/api/reviews/run/review-comment-preview", json={})).json()
        response = await client.patch("/api/reviews/run/findings/key-a/disposition",
            json={"status": "resolved", "reason": "已提交人工修复，尚未重新运行机器审查"})
        assert response.status_code == 200
        plan = (await client.get("/api/reviews/run/publication-plan")).json()
        copy = (await client.post("/api/reviews/run/review-comment-check")).json()
        audit = (await client.get("/api/reviews/run/report")).text
    assert "人工处理：已处理" in plan["user_report_markdown"]
    assert "时间：未记录" not in plan["user_report_markdown"]
    assert "尚未重新运行机器审查" in copy["review_comment_markdown"]
    assert preview["preview_markdown"] == original["review_comment_markdown"]
    assert copy["preview_markdown"] == plan["review_comment_markdown"]
    assert copy["preview_markdown"] != preview["preview_markdown"]
    assert plan["outcome"] == original["outcome"] and plan["inline_drafts"] == original["inline_drafts"]
    assert audit == task.report_markdown
    assert repository.get_task(task.id).task_runtime_report == task.task_runtime_report
