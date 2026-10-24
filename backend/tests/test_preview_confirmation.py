import asyncio
import copy
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import delete, func, select

from app.core.config import settings
from app.models.orm import PreviewInputCacheOrm, ReviewPreviewOrm, WorkerJobOrm
from app.models.review import ReviewPreviewRequest
from app.services.review.review_planner import DeterministicReviewPlanner
from app.tools.repo_indexer import RepoIndexer
from tests.support.review_plans import confirmation, plan_service as plan_service
from tests.support.runtime_harness import production_input as production_input
from tests.support.persistence import persistence as persistence


REQUEST = ReviewPreviewRequest(pr_url="https://github.com/o/r/pull/1")


def queue_size(service):
    with service._repository._session_factory() as session:
        return session.scalar(select(func.count()).select_from(WorkerJobOrm))


def change_saved(service, identity, mutate):
    with service._repository._session_factory.begin() as session:
        row = session.get(ReviewPreviewOrm, identity)
        value = copy.deepcopy(row.payload)
        mutate(value)
        row.payload = value


async def test_confirmation_never_prepares_or_replans_even_after_cache_eviction(plan_service, monkeypatch):
    service, _, _, calls, _ = plan_service
    preview = await service.preview(REQUEST)
    saved = service._repository.get_preview(preview.preview_id)
    assert saved["configuration"]["initial_plan"]["review_units"]
    with service._repository._session_factory.begin() as session:
        session.execute(delete(PreviewInputCacheOrm))
    service._github_tool.fetch_pr.reset_mock()
    for name in ("preview", "_prepare_preview_inputs"):
        monkeypatch.setattr(service, name, AsyncMock(side_effect=AssertionError("confirmation must not prepare")))
    monkeypatch.setattr(service._git_tool, "clone_and_diff", Mock(side_effect=AssertionError("must not clone")))
    monkeypatch.setattr(RepoIndexer, "execute", AsyncMock(side_effect=AssertionError("must not index")))
    monkeypatch.setattr(DeterministicReviewPlanner, "plan", Mock(side_effect=AssertionError("must not replan")))
    task = await service.create_confirmed_task(confirmation(preview))
    assert service._github_tool.fetch_pr.await_count == 1 and calls == []
    assert task.task_runtime_configuration == saved["configuration"]
    assert (await service.create_confirmed_task(confirmation(preview))).id == task.id
    assert service._github_tool.fetch_pr.await_count == 1 and queue_size(service) == 1
    assert service._repository.get_task(task.id).task_runtime_configuration["initial_plan"] == saved["configuration"]["initial_plan"]


@pytest.mark.parametrize("change", ["missing_binding", "future_protocol", "missing_plan", "plan", "response", "runtime", "binding"])
async def test_legacy_or_corrupt_preview_is_rejected_locally_without_work(plan_service, change):
    service, *_ = plan_service
    preview = await service.preview(REQUEST)
    def mutate(value):
        if change == "missing_binding":
            value.pop("confirmation")
        elif change == "future_protocol":
            value["confirmation"]["version"] = "future"
        elif change == "missing_plan":
            value["configuration"].pop("initial_plan")
        elif change == "plan":
            value["configuration"]["initial_plan"]["warnings"].append("tampered")
        elif change == "response":
            value["response"]["estimated_tokens"] += 1
        elif change == "runtime":
            value["configuration"]["limits"]["max_calls"] += 1
        else:
            value["confirmation"]["pr_fingerprint"] = "invalid"
    change_saved(service, preview.preview_id, mutate)
    service._github_tool.fetch_pr.reset_mock()
    with pytest.raises(ValueError, match="review_plan_(repreview_required|stale)"):
        await service.create_confirmed_task(confirmation(preview))
    assert service._github_tool.fetch_pr.await_count == 0
    assert service._repository.get_preview(preview.preview_id)["consumed_task_id"] is None and queue_size(service) == 0


@pytest.mark.parametrize("change", ["threshold", "rule_version", "tool_version", "diff_version",
                                  "diff_context", "price", "runtime", "server_limits"])
async def test_current_configuration_changes_require_repreview_without_github(plan_service, monkeypatch, change):
    service, *_ = plan_service
    preview = await service.preview(REQUEST)
    if change == "threshold":
        monkeypatch.setattr(settings, "repoguardian_review_unit_small_max_changed_lines",
                            settings.repoguardian_review_unit_small_max_changed_lines + 1)
    elif change == "rule_version":
        monkeypatch.setattr(settings, "repoguardian_rule_version", "changed")
    elif change == "tool_version":
        monkeypatch.setattr(settings, "repoguardian_tool_schema_version", "changed")
    elif change == "diff_version":
        monkeypatch.setattr("app.tools.git_tool.DIFF_POLICY_VERSION", "changed")
    elif change == "diff_context":
        monkeypatch.setattr("app.tools.git_tool.DIFF_CONTEXT_LINES", 80)
    elif change == "price":
        monkeypatch.setattr(settings, "repoguardian_model_pricing_json", '{"changed": {}}')
    elif change == "server_limits":
        monkeypatch.setattr(settings, "repoguardian_run_max_tokens", settings.repoguardian_run_max_tokens + 1)
    else:
        from app.review import runtime_spec
        original = runtime_spec.task_runtime_configuration
        monkeypatch.setattr(runtime_spec, "task_runtime_configuration", lambda *args, **kwargs:
            {**original(*args, **kwargs), "endpoint": "https://changed.invalid"})
    service._github_tool.fetch_pr.reset_mock()
    with pytest.raises(ValueError, match="review_plan_configuration_changed"):
        await service.create_confirmed_task(confirmation(preview))
    assert service._github_tool.fetch_pr.await_count == 0 and queue_size(service) == 0


@pytest.mark.parametrize("change", ["body", "hidden_body", "source", "branch"])
async def test_pr_planning_inputs_are_checked_without_loading_diff(plan_service, change):
    service, _, _, _, pr = plan_service
    pr.body = "a" * 10000
    preview = await service.preview(REQUEST)
    if change == "body":
        pr.body = "new intent"
    elif change == "hidden_body":
        pr.body = pr.body[:5000] + "b" + pr.body[5001:]
    elif change == "source":
        pr.head.repo_clone_url = "https://github.com/other/fork.git"
    else:
        pr.head.ref = "other-branch"
    with pytest.raises(ValueError, match="review_plan_stale"):
        await service.create_confirmed_task(confirmation(preview))
    assert service._repository.get_preview(preview.preview_id)["consumed_task_id"] is None and queue_size(service) == 0


async def test_configuration_change_during_metadata_request_is_rejected(plan_service, monkeypatch):
    service, _, _, _, pr = plan_service
    preview = await service.preview(REQUEST)
    async def fetch(url):
        monkeypatch.setattr(settings, "repoguardian_rule_version", "changed-during-request")
        return pr
    service._github_tool.fetch_pr.side_effect = fetch
    with pytest.raises(ValueError, match="review_plan_configuration_changed"):
        await service.create_confirmed_task(confirmation(preview))
    assert queue_size(service) == 0


async def test_invalid_current_model_profile_requires_repreview_without_exposing_details(plan_service, monkeypatch):
    service, *_ = plan_service
    preview = await service.preview(REQUEST)
    monkeypatch.setattr("app.review.runtime_spec.task_runtime_configuration",
        Mock(side_effect=ValueError("private configuration details")))
    service._github_tool.fetch_pr.reset_mock()
    with pytest.raises(ValueError, match="^review_plan_configuration_changed$"):
        await service.create_confirmed_task(confirmation(preview))
    assert service._github_tool.fetch_pr.await_count == 0 and queue_size(service) == 0


async def test_consumed_legacy_preview_returns_original_task_without_revalidation(plan_service):
    service, *_ = plan_service
    preview = await service.preview(REQUEST)
    task = await service.create_confirmed_task(confirmation(preview))
    def strip_new_fields(value):
        value.pop("confirmation")
        value["configuration"].pop("initial_plan")
    change_saved(service, preview.preview_id, strip_new_fields)
    service._github_tool.fetch_pr.side_effect = AssertionError("replay must not fetch GitHub")
    assert (await service.create_confirmed_task(confirmation(preview))).id == task.id
    assert queue_size(service) == 1


async def test_concurrent_confirmations_create_at_most_one_task(plan_service):
    service, _, _, calls, pr = plan_service
    preview = await service.preview(REQUEST)
    gate = asyncio.Event()
    count = 0
    async def fetch(url):
        nonlocal count
        count += 1
        if count == 3:
            gate.set()
        await asyncio.wait_for(gate.wait(), timeout=5)
        return pr
    service._github_tool.fetch_pr.side_effect = fetch
    results = await asyncio.gather(*(service.create_confirmed_task(confirmation(preview)) for _ in range(3)),
                                   return_exceptions=True)
    tasks = [value for value in results if not isinstance(value, BaseException)]
    assert tasks and len({task.id for task in tasks}) == 1
    assert all(not isinstance(value, BaseException) or isinstance(value, ValueError) for value in results)
    assert service.list_tasks(status=None, page=1, page_size=20).total == queue_size(service) == 1 and calls == []


@pytest.mark.parametrize("change", ["head", "base", "effective_base", "actual_plan"])
async def test_execution_still_rejects_snapshot_or_actual_plan_drift(plan_service, monkeypatch, change):
    from app.graph.nodes import repo_prepare
    from app.graph.nodes.review_units import review_plan_node
    service, state, _, calls, pr = plan_service
    preview = await service.preview(REQUEST)
    task = await service.create_confirmed_task(confirmation(preview))
    configuration = task.task_runtime_configuration
    if change == "actual_plan":
        # 冻结initial_plan仍在，实际源码目标改变必须被重规划校验拒绝。
        with pytest.raises(ValueError, match="deterministic plan changed"):
            await review_plan_node({**state, "model": "fixture", "changed_files": [],
                                   "task_runtime_configuration": configuration})
    else:
        monkeypatch.setattr(repo_prepare, "get_stream_writer", lambda: lambda event: None)
        if change in {"head", "base"}:
            getattr(pr, change).sha = "f" * 40
            monkeypatch.setattr(service._git_tool, "clone_and_diff", Mock(side_effect=AssertionError("stale commit must not clone")))
        else:
            monkeypatch.setattr(service._git_tool, "review_diff_context", lambda *args: {"effective_base_sha": "f" * 40})
        with pytest.raises(ValueError, match="review_plan_stale"):
            await repo_prepare.repo_prepare_node({"pr_url": str(REQUEST.pr_url), "_github_tool": service._github_tool,
                "_git_tool": service._git_tool, "task_runtime_configuration": configuration})
    assert calls == []
