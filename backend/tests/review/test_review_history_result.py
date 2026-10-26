import pytest

from app.api import reviews
from app.models.persistence import ReviewTaskListResponse
from app.models.review import TaskStatus
from app.services.publication.publication_planner import PublicationPlanner
from tests.support.review_tasks import publishable_task as publishable_task
from tests.support.review_tasks import completed_task as completed_task


@pytest.mark.asyncio
async def test_history_projects_saved_results_without_markdown_or_remote_calls(monkeypatch):
    tasks = [completed_task(), publishable_task(severity="high", claim="权限绕过导致未授权数据访问"),
             completed_task(scope_complete=False), completed_task(),
             completed_task(), completed_task()]
    tasks[1].task_runtime_report["findings"][0]["issue"]["confidence"] = 0.95
    tasks[3].status = TaskStatus.failed
    tasks[4].status = TaskStatus.cancelled
    tasks[5] = tasks[5].model_validate({**tasks[5].model_dump(), "engine": "legacy_units"})
    before = [task.model_dump(mode="json") for task in tasks]
    response = ReviewTaskListResponse(items=tasks, total=len(tasks), page=1, page_size=12)

    class Service:
        def list_tasks(self, **kwargs):
            assert kwargs["page_size"] == 12
            return response

    monkeypatch.setattr(reviews, "get_review_service", lambda: Service())
    import app.services.publication.publication_planner as planner
    expected = [PublicationPlanner().build(task) for task in tasks]
    monkeypatch.setattr(planner, "render_summary", lambda *_: pytest.fail("history rendered Markdown"))
    monkeypatch.setattr(planner, "render_user_report", lambda *_: pytest.fail("history rendered Markdown"))
    monkeypatch.setattr(planner, "render_review_comment", lambda *_: pytest.fail("history rendered Markdown"))
    result = await reviews.list_reviews(task_status=None, page=1, page_size=12,
                                       q=None, repository=None, pr_number=None, head_sha=None)
    for item, plan in zip(result.items, expected):
        assert item.history_result.outcome == plan.outcome
        assert item.history_result.merge_decision == plan.digest.merge_decision
    assert result.items[1].history_result.merge_decision.status == "fix_blockers"
    assert result.items[2].history_result.outcome.kind == "inconclusive"
    assert result.items[5].history_result.outcome.kind == "inconclusive"
    assert [task.model_dump(mode="json") for task in tasks] == before
    assert "history_result" in result.model_dump(mode="json")["items"][0]
