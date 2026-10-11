from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.graph.nodes.report import complete_node, report_node
from app.graph.nodes.review_units import review_units_node
from app.models.review import (
    AgentAction, ContextSnippet, CrossUnitCoordinationPlan, IssueStatus, ReviewIssue,
    ReviewUnitResult, UnitReviewPlan, UnitReviewSummary,
)
from app.review.unit_completion import (
    is_reusable_review_unit_result, is_review_unit_complete, is_review_unit_execution_complete,
)
from app.services.review_manifest import build_review_manifest
from test_unit_diagnosis_holdback import BoundedProvider, executor, unit
from test_unit_review_summary import _state


def planned_state():
    state = _state()
    state["review_units"] = [unit().model_dump(mode="json")]
    state["review_plan"] = {"planner_version": "fixture", "review_units": state["review_units"], "changed_files": [
        {key: item[key] for key in ("file_path", "change_type", "additions", "deletions")}
        | {"included": True} for item in state["changed_files"]]}
    return state


def degraded_executor(candidate=False, record_status="reported"):
    class Provider(BoundedProvider):
        async def plan_review_unit(self, state, model):
            await self.reserve("plan", 1000)
            return UnitReviewPlan(change_summary="核验调用方", review_objectives=["验证作者声明"],
                coverage_targets=["核验兼容性"], risk_hypotheses=[{
                    "id": "h", "category": "correctness", "priority": "high",
                    "description": "核验调用方契约", "completion_criteria": "来源可核对"}],
                initial_action=AgentAction(action="report_issue", reason="诊断"))

        async def review_unit(self, *args):
            result = await super().review_unit(*args)
            if candidate:
                result.value.issues = [ReviewIssue(id="candidate", review_unit_id="u", title="空值问题", severity="high",
                    category="correctness", confidence=.9, affected_behavior="读取字段失败",
                    failure_scenario="输入为空", recommendation="处理空值",
                    primary_evidence={"file_path": "a.py", "existing_code": "return value"})]
            if record_status == "missing":
                result.value.review_record = None
            elif record_status == "invalid":
                result.value.review_record.target_checks[0].evidence_ids = ["stale"]
            return result
    return executor(Provider())


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate", [False, True])
@pytest.mark.parametrize("coordination", ["not_required", "completed"])
async def test_degraded_diagnosis_retains_candidates_but_reports_partial_coverage(candidate, coordination):
    state = planned_state()
    state["_review_unit_executor"] = degraded_executor(candidate)
    aggregate = await review_units_node(state)
    value = {**state, **aggregate}
    if coordination == "completed":
        value["coordination_plan"] = CrossUnitCoordinationPlan(status="completed").model_dump(mode="json")
    else:
        value["cross_unit_risk"] = {"decision": "skip", "non_execution_reason": "无需协调"}
    result = ReviewUnitResult.model_validate(aggregate["review_unit_results"][0])
    assert result.status == "completed", result.error
    assert is_review_unit_execution_complete(result) and not is_review_unit_complete(result)
    assert not is_reusable_review_unit_result(result)
    assert result.input_coverage.evidence_coverage == "complete"
    assert result.input_coverage.target_coverage == "partial"
    assert result.input_coverage.omitted_plan.review_objectives == ["验证作者声明"]
    assert "核验兼容性" in result.input_coverage.omitted_targets
    assert len(aggregate["review_issues"]) == int(candidate)
    assert len(result.issues) == int(candidate)
    manifest = build_review_manifest(value, datetime.now(timezone.utc))
    assert manifest.coverage.review_complete is False
    assert manifest.coverage.completed_units == manifest.coverage.reviewed_files == 0
    assert manifest.coverage.partial_files == 1 and manifest.coverage.failed_files == 0
    assert "核验兼容性" in manifest.coverage.files[0].omitted_targets
    assert manifest.coverage.units[0].status == "completed"
    assert manifest.coverage.units[0].input_coverage == result.input_coverage
    assert any("diagnosis_background_budget_degraded" in item for item in manifest.warnings)
    report = await report_node(value)
    assert report["status"] == (await complete_node(value))["status"] == "completed_with_warnings"
    assert "整体审查：未完整完成" in report["report_markdown"]
    assert "省略目标：核验兼容性" in report["report_markdown"]
    assert report["warnings"] == report["run_manifest"]["warnings"]


@pytest.mark.asyncio
@pytest.mark.parametrize("record_status", ["missing", "invalid"])
async def test_degraded_missing_or_invalid_record_preserves_attempt_reason_and_gap(record_status):
    result = await degraded_executor(True, record_status).execute_unit(unit(), _state())
    assert result.review_summary.latest_attempt_status == record_status, result.error
    assert result.review_summary.reason != "diagnosis_background_budget_degraded"
    assert result.input_coverage.target_coverage == "unknown"
    assert len(result.issues) == 1 and not is_review_unit_complete(result)


@pytest.mark.parametrize("marker", ["plan_skip_reason", "summary_reason", "attempt_reason"])
def test_old_degraded_and_missing_snapshots_remain_incomplete(marker):
    raw = {"review_unit_id": "u", "status": "completed", "terminal_reason": "no_issue"}
    if marker == "plan_skip_reason":
        raw[marker] = "diagnosis_background_budget_degraded"
    else:
        raw["review_summary"] = {"reason" if marker == "summary_reason" else "latest_attempt_reason":
            "diagnosis_background_budget_degraded"}
    result = ReviewUnitResult.model_validate(raw)
    state = planned_state() | {"review_unit_results": [result.model_dump(mode="json")],
                               "coordination_plan": {"status": "completed"}}
    manifest = build_review_manifest(state, datetime.now(timezone.utc))
    assert not manifest.coverage.review_complete and manifest.warnings
    assert manifest.coverage.units[0].input_coverage.evidence_coverage == "unknown"
    for summary in (UnitReviewSummary(), UnitReviewSummary(input_protocol="legacy")):
        historical = ReviewUnitResult(review_unit_id="u", status="completed", review_summary=summary)
        assert not is_review_unit_complete(historical)
        assert not is_reusable_review_unit_result(historical)


@pytest.mark.asyncio
async def test_report_reconstructs_warning_from_snapshot_without_upstream_warning():
    state = planned_state()
    result = await degraded_executor().execute_unit(unit(), state)
    state["review_unit_results"] = [result.model_dump(mode="json")]
    state["cross_unit_risk"] = {"decision": "skip"}
    assert not state.get("warnings")
    report = await report_node(state)
    assert report["status"] == "completed_with_warnings"
    assert report["warnings"] and "diagnosis_background_budget_degraded" in report["report_markdown"]


@pytest.mark.asyncio
async def test_legacy_human_unit_does_not_pause_or_drop_other_candidates_and_context():
    state = planned_state()
    result = await degraded_executor(True).execute_unit(unit(), state)
    result.context_snippets = [ContextSnippet(file="a.py", start_line=1, end_line=1,
        content="return value", relevance="direct", review_unit_id="u")]
    other = unit().model_copy(update={"id": "other"})
    state["review_plan"]["review_units"].append(other.model_dump(mode="json"))
    class Results:
        async def execute(self, units, state):
            return [result, ReviewUnitResult(review_unit_id="other", status="needs_human")]
    state["_review_unit_executor"] = Results()
    aggregate = await review_units_node(state)
    assert aggregate["next_action"] is None
    assert aggregate["review_issues"][0]["id"] == "candidate"
    assert aggregate["context_snippets"][0]["content"] == "return value"
    assert aggregate["warnings"]


@pytest.mark.asyncio
async def test_resumed_degraded_unit_retains_gap_when_next_diagnosis_is_rejected():
    state = planned_state()
    runner = degraded_executor(True)
    result = await runner.execute_unit(unit(), state)
    state["review_unit_results"] = [result.model_dump(mode="json")]
    resumed = await runner.execute_unit(unit(), state)
    assert resumed.status == "failed"
    assert resumed.input_coverage == result.input_coverage
    assert resumed.input_coverage.omitted_plan is not None
    assert not is_review_unit_complete(resumed) and not is_reusable_review_unit_result(resumed)


@pytest.mark.asyncio
async def test_single_unit_retry_verifies_degraded_candidate_and_rebuilds_coverage(monkeypatch, tmp_path):
    from app.services import review_service as module
    from app.services.review_rebuild import rebuild_task_from_state
    from app.services.report_service import ReportService
    from test_unit_review_summary import _file

    state = planned_state()
    result = await degraded_executor(True).execute_unit(unit(), state)
    task = rebuild_task_from_state({**state, "status": "completed"})
    service = module.ReviewService(None, SimpleNamespace(clone_and_diff=lambda pr: (tmp_path, "diff")),
        SimpleNamespace(parse=lambda diff: [_file()]), BoundedProvider(), ReportService())
    service._tasks[task.id] = task
    monkeypatch.setattr(module, "ReviewUnitExecutor", lambda *args, **kwargs:
        SimpleNamespace(execute_unit=AsyncMock(return_value=result)))
    monkeypatch.setattr(module.RepoIndexer, "execute", AsyncMock(return_value={
        "file_index": state["file_index"], "symbol_index": [], "repository_graph": {}}))
    monkeypatch.setattr(module, "_cleanup_repo", AsyncMock())
    stages = []
    async def resolve(value):
        stages.append("resolve")
        assert value["review_issues"][0]["id"] == "candidate"
        return {}
    async def policy(value):
        stages.append("policy")
        return {}
    async def verify(value):
        stages.append("verify")
        verified = result.model_copy(update={"issues": [result.issues[0].model_copy(update={"status": IssueStatus.confirmed})]})
        return {"review_unit_results": [verified.model_dump(mode="json")]}
    monkeypatch.setattr(module, "resolve_evidence_node", resolve)
    monkeypatch.setattr(module, "issue_policy_node", policy)
    monkeypatch.setattr(module, "issue_verifier_node", verify)
    async def aggregate(self, issues, provider, model, metrics):
        return SimpleNamespace(issues=issues, metrics=metrics)
    monkeypatch.setattr(module.IssueDeduplicationService, "aggregate", aggregate)
    retried = await service.retry_unit(task.id, "u")
    assert stages == ["resolve", "policy", "verify"]
    assert retried.issues[0].status == "confirmed" and task.issues[0].id == "candidate"
    assert task.status == "completed_with_warnings" and not task.coverage.review_complete
    assert task.coverage.partial_files == 1 and task.run_manifest.warnings == task.warnings
    assert "diagnosis_background_budget_degraded" in task.report_markdown
