from copy import deepcopy
from datetime import datetime, timedelta, timezone
import time

import pytest
from langchain_core.messages import AIMessage

from app.models.review import ReviewPreviewRequest
from app.models.review_plan import ReviewPlanOptions
from app.services.review_continuation import continuation
from app.services.review_run_budget import ReviewRunBudget
from test_review_plans import plan_service as plan_service, confirmation
from test_phase6a_persistence import persistence as persistence
from test_task_runtime_p4 import production_input as production_input


async def saved_v2(plan_service, monkeypatch, *, blocked=False):
    import json
    from app.graph.nodes.task_review_runtime import task_review_runtime_node
    from app.services.review_manifest import build_review_manifest
    from app.tools.repo_indexer import RepoIndexer
    service, state, provider, _, _ = plan_service
    from app.graph.nodes import task_review_runtime
    original = task_review_runtime.task_runtime_configuration
    monkeypatch.setattr(task_review_runtime, "task_runtime_configuration", lambda *a, **k:
        {**original(*a, **k), "stability_contract": "review-stability-v2", "deadline_seconds": 600})
    request = ReviewPreviewRequest(pr_url=state["pr_url"], plan=ReviewPlanOptions(include_pr_discussions=False))
    preview = await service.preview(request)
    task = await service.create_confirmed_task(confirmation(preview))
    if blocked:
        class SDK:
            async def ainvoke(self, messages):
                payload = json.loads(messages[1].content.splitlines()[0])
                return AIMessage(content=json.dumps({"kind": "submit", "examinations": [
                    {"target_id": goal["goal_id"], "disposition": "blocked", "evidence_ids": [],
                     "reason": "缺少实质调用契约"} for goal in payload["goals"]]}),
                    usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
        provider._build_chat_model = lambda *args: SDK()
    state.update(task_id=task.id, task_runtime_configuration=task.task_runtime_configuration,
                 effective_base_sha=preview.snapshot.effective_base_sha,
                 review_plan=task.task_runtime_configuration["initial_plan"],
                 _provider=provider, _coordination_repository=service._repository)
    state.update(await RepoIndexer().execute(repo_path=state["repo_path"], call_resolution_version="import-evidence-v5"))
    result = await task_review_runtime_node(state)
    task.task_runtime_report = result["task_runtime_report"]
    from app.models.review import ReviewUnit, ChangedFile
    task.pr = deepcopy(service._github_tool.fetch_pr.return_value)
    task.review_units = [ReviewUnit.model_validate(value) for value in state["review_plan"]["review_units"]]
    task.changed_files = [ChangedFile.model_validate(value) for value in state["changed_files"]]
    task.status = type(task.status)(result["status"])
    task.run_manifest = build_review_manifest({**state, **result}, datetime.now(timezone.utc))
    service._repository.save_task(task)
    return service, task, request


async def test_default_saved_result_and_force_full_are_bound_to_confirmation(plan_service, monkeypatch):
    service, task, request = await saved_v2(plan_service, monkeypatch)
    ordinary = await service.preview(request)
    assert ordinary.existing_review_id == task.id
    full = await service.preview(request.model_copy(update={"force_full_review": True}))
    assert full.existing_review_id is None and full.continuation is None
    assert not service._repository.get_preview(full.preview_id)["configuration"].get("incremental")
    with pytest.raises(ValueError, match="review_plan_stale"):
        await service.create_confirmed_task(confirmation(full))
    created = await service.create_confirmed_task(confirmation(full).model_copy(update={"force_full_review": True}))
    assert created.id != task.id and not created.task_runtime_configuration.get("incremental")


async def test_latest_cancelled_record_is_not_hidden_by_an_older_complete_result(plan_service, monkeypatch):
    service, task, request = await saved_v2(plan_service, monkeypatch)
    cancelled = task.model_copy(deep=True, update={"id": "c" * 32, "status": type(task.status).cancelled,
        "created_at": datetime.now(timezone.utc)})
    service._repository.create_task(cancelled)
    service._repository.save_task(cancelled)
    preview = await service.preview(request)
    assert preview.existing_review_id is None
    assert preview.continuation["review_id"] == cancelled.id
    assert not preview.continuation["can_resume"] and preview.continuation["reason"] == "cancelled"


async def test_resume_consumes_preview_once_without_resetting_original_ledger(plan_service, monkeypatch):
    service, task, request = await saved_v2(plan_service, monkeypatch, blocked=True)
    before = service._repository.load_run_budget(task.id)
    preview = await service.preview(request)
    assert preview.continuation["can_resume"] and preview.continuation["review_id"] == task.id
    result = await service.resume_review(task.id, confirmation(preview))
    assert result.requeued_work_item_ids
    assert service._repository.get_task(task.id).status.value == "queued"
    assert service._repository.load_run_budget(task.id) == before
    assert service._repository.get_preview(preview.preview_id)["consumed_task_id"] == task.id
    assert not (await service.resume_review(task.id, confirmation(preview))).requeued_work_item_ids
    assert service._repository.load_run_budget(task.id) == before


@pytest.mark.parametrize("failure", ["head", "body", "enqueue"])
async def test_resume_rejects_changed_identity_and_rolls_back_failed_enqueue(plan_service, monkeypatch, failure):
    service, task, request = await saved_v2(plan_service, monkeypatch, blocked=True)
    preview = await service.preview(request)
    before = service._repository.load_run_budget(task.id)
    if failure == "enqueue":
        from app.services.task_queue import DatabaseTaskQueue
        def reject(*args, **kwargs):
            raise RuntimeError("fixture enqueue failure")
        monkeypatch.setattr(DatabaseTaskQueue, "enqueue", reject)
    else:
        changed = deepcopy(service._github_tool.fetch_pr.return_value)
        if failure == "head":
            changed.head.sha = "f" * 40
        else:
            changed.body = "new background"
        service._github_tool.fetch_pr.return_value = changed
    with pytest.raises((ValueError, RuntimeError)):
        await service.resume_review(task.id, confirmation(preview))
    assert service._repository.get_task(task.id).status == task.status
    assert service._repository.get_preview(preview.preview_id)["consumed_task_id"] is None
    assert service._repository.load_run_budget(task.id) == before


@pytest.mark.parametrize("failure,reason", [("deadline", "deadline"), ("unknown", "unknown_attempts"),
    ("budget", "budget_exhausted"), ("cost", "budget_exhausted"), ("cancelled", "cancelled")])
async def test_continuation_preserves_resource_and_unknown_boundaries(failure, reason):
    from app.models.review import ReviewTask
    task = ReviewTask(id="r", pr_url="https://github.com/o/r/pull/1", engine="task_review_v1",
        status="failed", created_at=datetime.now(timezone.utc), task_runtime_configuration={"deadline_seconds": 60},
        task_runtime_report={"execution_status": "failed", "termination_reason": "model_response_rejected",
                            "initial_scope_coverage": [{"goal_id": "g", "status": "not_examined"}],
                            "work_items": [{"work_item_id": "w", "unit_id": "u", "goal_ids": ["g"], "state": "failed"}]})
    budget = ReviewRunBudget("r", max_calls=2, max_tokens=100)
    checkpoint = {"spec": {"run_id": "r"}, "deadline_at": time.time() + 60}
    if failure == "deadline":
        task.created_at -= timedelta(seconds=120)
    elif failure == "unknown":
        attempt = await budget.reserve(request_id="x", operation="review", estimated_tokens=1)
        await budget.mark_dispatching(attempt.attempt_id)
        await budget.settle(attempt.attempt_id, actual_tokens=None)
    elif failure == "budget":
        budget.max_calls = 0
    elif failure == "cost":
        budget.max_cost_microusd = 0
    else:
        checkpoint["cancelled"] = True
    value = continuation(task, checkpoint, budget.snapshot())
    assert not value["can_resume"] and value["reason"] == reason


def test_failed_status_alone_is_not_a_retryable_evidence_gap():
    from app.models.review import ReviewTask
    task = ReviewTask(id="r", pr_url="https://github.com/o/r/pull/1", engine="task_review_v1", status="failed",
        task_runtime_configuration={"deadline_seconds": 60}, task_runtime_report={
            "execution_status": "failed", "termination_reason": "model_response_rejected",
            "initial_scope_coverage": [{"goal_id": "g", "status": "examined"}],
            "work_items": [{"work_item_id": "w", "unit_id": "u", "goal_ids": ["g"], "state": "failed", "retryable_gap": False}]})
    checkpoint = {"spec": {"run_id": "r"}, "deadline_at": time.time() + 60}
    ledger = ReviewRunBudget("r", max_calls=2, max_tokens=100).snapshot()
    assert continuation(task, checkpoint, ledger)["reason"] == "no_retryable_gap"
    task.task_runtime_report["work_items"][0]["retryable_gap"] = True
    assert continuation(task, checkpoint, ledger)["can_resume"]


@pytest.mark.parametrize("reason", ["deadline", "budget_exhausted", "unknown_attempts"])
async def test_resume_api_returns_actionable_resource_refusal(monkeypatch, reason):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from httpx import ASGITransport, AsyncClient
    from app.api import reviews
    from app.main import app
    service = SimpleNamespace(resume_review=AsyncMock(
        side_effect=ValueError("review_resume_unavailable:" + reason)))
    monkeypatch.setattr(reviews, "get_review_service", lambda: service)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/reviews/run/resume",
            json={"pr_url": "https://github.com/o/r/pull/1"})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "review_resume_unavailable" and detail["reason"] == reason
    assert detail["message"] != reason
