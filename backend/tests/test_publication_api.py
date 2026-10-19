from types import SimpleNamespace

import httpx
import pytest

from app.api import reviews
from app.main import app
from app.models.publication import PublicationPlan
from app.models.review import ReviewTask, TaskStatus
from test_publication_planner import publishable_task
from test_review_outcome import completed_task


async def fetch_plan(monkeypatch, task, task_id="run"):
    # 唯一依赖为任务读取；Fake 不提供任何模型、GitHub 或写入方法。
    monkeypatch.setattr(reviews, "get_review_service", lambda: SimpleNamespace(get_task=lambda identity: task if identity == task_id else None))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(f"/api/reviews/{task_id}/publication-plan")


async def test_plan_endpoint_is_read_only_repeatable_and_schema_valid(monkeypatch):
    task = publishable_task()
    original = task.model_dump(mode="json")
    first = await fetch_plan(monkeypatch, task)
    assert first.status_code == 200
    plan = PublicationPlan.model_validate(first.json())
    assert plan.outcome.publishable_count == len(plan.inline_drafts) == 1
    assert first.json() == (await fetch_plan(monkeypatch, task)).json()
    assert task.model_dump(mode="json") == original
    restored = ReviewTask.model_validate(original)
    assert first.json() == (await fetch_plan(monkeypatch, restored)).json()


@pytest.mark.parametrize("status", list(TaskStatus))
async def test_endpoint_terminal_and_unfinished_statuses(monkeypatch, status):
    task = completed_task()
    task.status = status
    response = await fetch_plan(monkeypatch, task)
    terminal = status in {TaskStatus.completed, TaskStatus.completed_with_warnings, TaskStatus.failed, TaskStatus.cancelled}
    assert response.status_code == (200 if terminal else 409)
    if terminal:
        # 任务标签不能覆盖本 fixture 已保存的完整、可靠 Runtime 结论。
        assert response.json()["outcome"]["kind"] == {"failed": "failed", "cancelled": "cancelled"}.get(status, "clean")
    else:
        assert response.json()["detail"] == "Review is not finished"


async def test_missing_task_is_404(monkeypatch):
    response = await fetch_plan(monkeypatch, None)
    assert response.status_code == 404 and response.json()["detail"] == "Task not found"


@pytest.mark.parametrize("problem", ["legacy", "missing", "invalid", "unknown", "failed", "cancelled"])
async def test_historical_incomplete_tasks_always_get_conservative_plan(monkeypatch, problem):
    task = completed_task()
    if problem == "legacy":
        task = ReviewTask(id="run", pr_url=task.pr_url, status="completed", report_markdown="历史审计报告")
    elif problem == "unknown":
        task.task_runtime_report["findings"] = [{"status": ["future"], "candidate": "old-format"}]
    else:
        task.task_runtime_report = {"execution_status": "old-format"} if problem == "invalid" else None
        if problem in {"failed", "cancelled"}:
            task.status = problem
    response = await fetch_plan(monkeypatch, task)
    assert response.status_code == 200
    plan = response.json()
    assert plan["outcome"]["kind"] == (problem if problem in {"failed", "cancelled"} else "inconclusive")
    assert not plan["inline_drafts"] and not plan["summary_findings"]
    if problem == "legacy":
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/api/reviews/run/report")).text == "历史审计报告"


async def fetch_summary(monkeypatch, task, task_id="run"):
    monkeypatch.setattr(reviews, "get_review_service", lambda: SimpleNamespace(get_task=lambda identity: task if identity == task_id else None))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(f"/api/reviews/{task_id}/publication-summary")


async def test_summary_endpoint_is_a_light_projection_of_the_same_plan(monkeypatch):
    from app.models.publication import ReviewSummary
    task = publishable_task()
    original = task.model_dump(mode="json")
    response = await fetch_summary(monkeypatch, task)
    assert response.status_code == 200
    summary = ReviewSummary.model_validate(response.json())
    plan = (await fetch_plan(monkeypatch, task)).json()
    assert summary.review_id == plan["review_id"]
    assert response.json()["outcome"] == plan["outcome"]
    assert summary.review_comment_markdown == plan["review_comment_markdown"]
    assert summary.digest.merge_decision.model_dump(mode="json") == plan["digest"]["merge_decision"]
    assert "inline_drafts" not in response.json() and "user_report_markdown" not in response.json()
    for key in ("execution_record", "findings", "examined_goals", "incomplete_goals"):
        assert key not in response.json()["digest"]
    assert task.model_dump(mode="json") == original


async def test_summary_endpoint_rejects_unfinished_and_missing_tasks(monkeypatch):
    task = completed_task()
    task.status = TaskStatus.reviewing
    assert (await fetch_summary(monkeypatch, task)).status_code == 409
    assert (await fetch_summary(monkeypatch, None)).status_code == 404


def test_openapi_exposes_full_publication_schema_and_errors():
    schema = app.openapi()
    endpoint = schema["paths"]["/api/reviews/{task_id}/publication-plan"]
    assert set(endpoint) == {"get"}
    assert {"200", "404", "409"} <= set(endpoint["get"]["responses"])
    assert schema["components"]["schemas"]["PublicationPlan"]["properties"]["schema_version"]["const"] == "review-publication-v1"
    summary = schema["paths"]["/api/reviews/{task_id}/publication-summary"]
    assert {"200", "404", "409"} <= set(summary["get"]["responses"])
    schemas = schema["components"]["schemas"]
    review_summary = next(value for name, value in schemas.items() if name.endswith("__ReviewSummary") and
                          name.startswith("app__models__publication__"))
    assert review_summary["properties"]["schema_version"]["const"] == "review-summary-v1"
    assert "execution_record" not in schemas["ReviewSummaryDigest"]["properties"]
