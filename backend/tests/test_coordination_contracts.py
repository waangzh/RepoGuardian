import json

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from app.agents.providers import LLMProviderError, OpenAICompatibleProvider
from app.models.review import (
    CrossUnitCoordinationPlan, CrossUnitCoordinationProposal, ExecutionBudget, IssueVerification,
    IssueVerificationBudget, IssueVerificationRequest, ModelUsage, ModelCallResult,
)
from app.services.coordination_runtime import CoordinationRuntime, estimate_request
from app.services.cross_unit_coordination import CrossUnitCoordinationService
from app.services.issue_verifier import IssueVerifierService
from app.services.unit_review_summary import validate_record
from test_cross_unit_coordination import Provider as FollowupProvider, plan, state
from test_issue_validation import _issue, _state, _unit
from test_provider import fake_chat as fake_chat, _sample_pr


@pytest.mark.parametrize("units,decision", [
    (["u0"], "required"), (["u0", "u0"], "required"),
    (["u0", "u1"], "uncertain"), (["u0", "u1"], "skip"),
])
def test_followup_structure_rejects_single_unit_and_non_required_decision(units, decision):
    proposed = plan(CrossUnitCoordinationService.catalog(state())).model_dump(mode="json")
    proposed["decision"] = decision
    proposed["followups"][0]["unit_ids"] = units
    with pytest.raises(ValidationError):
        CrossUnitCoordinationProposal.model_validate(proposed)


def test_legacy_invalid_terminal_plan_stays_readable_but_cannot_be_reexecuted():
    payload = CrossUnitCoordinationService.catalog(state())
    proposed = plan(payload).model_dump(mode="json")
    proposed.update(status="failed", decision="uncertain")
    proposed["followups"][0]["unit_ids"] = ["u0"]
    saved = CrossUnitCoordinationPlan.model_validate(proposed)
    assert saved.status == "failed"
    with pytest.raises(ValueError):
        CrossUnitCoordinationService.validate_plan(saved, payload)


@pytest.mark.asyncio
async def test_prompt_budget_ignores_parent_state_that_is_not_sent(fake_chat):
    # Unit 参数保留完整 hunk，实际决策提示只发送路径和裁剪后的 unit_diff。
    parent = {"unit_agent": True, "unit_diff": "return wrong", "changed_files": [
        {"file_path": "app.py", "hunks": [{"content": "x" * 600_000}]},
    ]}
    fake_chat.responses = [AIMessage(content='{"action":"task_done","reason":"完成"}')]
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")
    runtime = CoordinationRuntime("task", "fingerprint", ExecutionBudget(max_token_usage=10_000))
    result = await runtime.call(provider, "decide", (parent, None), 1200)
    assert result.action == "task_done"
    assert len(fake_chat.instances) == 1
    assert "xxx" not in fake_chat.instances[0].messages[1].content
    call = next(iter(runtime.data["calls"].values()))
    assert call["estimate_source"] == "provider_prompt"
    assert runtime.budget.token_usage < 10_000
    assert call["input_chars"] == len(fake_chat.instances[0].messages[1].content) + 512
    # 幂等恢复不会重新预留或请求。
    await runtime.call(provider, "decide", (parent, None), 1200)
    assert runtime.budget.model_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("dimension", ["model_calls", "token_usage"])
async def test_budget_rejection_identifies_operation_and_does_not_call_model(dimension):
    budget = ExecutionBudget(**{"max_" + dimension: 0})
    runtime = CoordinationRuntime("task", "fingerprint", budget)
    class Provider:
        async def decide(self, *args):
            raise AssertionError("预算拒绝后不应调用模型")
    with pytest.raises(LLMProviderError, match="cross_unit_shared_budget_exhausted"):
        await runtime.call(Provider(), "decide", ({}, None), 1200)
    rejection = runtime.data["budget_rejections"][0]
    assert rejection["operation"] == "decide"
    assert rejection["exhausted_dimensions"] == [dimension]
    assert rejection["used_tokens"] == 0
    assert runtime.budget.model_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["provider", "runtime_schema"])
async def test_schema_failure_still_corrects_reported_usage_and_blocks_later_calls(failure):
    usage = ModelUsage(provider="test", model="model", operation="decide", latency_ms=1,
                       actual_total_tokens=20_000, usage_available=True)
    class Provider:
        async def decide(self, *args):
            if failure == "runtime_schema":
                return ModelCallResult({"action": "invalid", "reason": "错误"}, usage)
            raise LLMProviderError("schema rejected", usage=usage)
    runtime = CoordinationRuntime("task", "fingerprint", ExecutionBudget(max_token_usage=10_000))
    with pytest.raises((LLMProviderError, ValidationError)):
        await runtime.call(Provider(), "decide", ({}, None), 1200)
    assert runtime.budget.token_usage == 20_000
    call = next(iter(runtime.data["calls"].values()))
    assert call["correction_tokens"] == 20_000 - call["estimate"]
    with pytest.raises(LLMProviderError, match="shared_budget_exhausted"):
        await runtime.call(Provider(), "decide", ({"next": True}, None), 1200)


@pytest.mark.asyncio
async def test_failed_followup_is_not_counted_as_completed_and_preserves_budget_source():
    class Provider(FollowupProvider):
        def coordination_request_chars(self, name, args):
            return 1_000_000 if name == "decide" else 1_000
    provider = Provider()
    result = await CrossUnitCoordinationService(provider).run(state())
    assert provider.calls == ["coordinate"]
    followup = result["followup_results"][0]
    assert followup["outcome"] == "failed"
    assert '"operation": "decide"' in followup["unit_result"]["error"]
    assert '"exhausted_dimensions": ["token_usage"]' in followup["unit_result"]["error"]
    assert result["coordination_plan"]["status"] == "unresolved"
    assert result["coordination_plan"]["runtime_metrics"]["completed_followups"] == 0


@pytest.mark.asyncio
async def test_review_record_unknown_fields_keep_issues_and_safe_error_paths(fake_chat):
    issue = _issue("i").model_dump(include={"title", "category", "severity", "confidence",
        "affected_behavior", "failure_scenario", "recommendation"})
    issue["primary_evidence"] = {"file_path": "app.py", "existing_code": "return wrong"}
    record = {"change_summary": "修改返回值", "target_checks": [{
        "target": "检查返回值", "status": "unresolved", "reason": "缺证据",
        "private-text-do-not-export": "secret-value",
    }]}
    fake_chat.responses = [AIMessage(content=json.dumps({"issues": [issue], "review_record": record}))]
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")
    catalog = {"targets": ["检查返回值"], "hypotheses": [], "evidence": []}
    response = await provider.review_unit(_sample_pr(), [], "", None, catalog)
    assert len(response.issues) == 1
    assert response.review_record is None
    errors = json.loads(response.record_error.split(": ", 1)[1])
    assert errors == [{"type": "extra_forbidden", "loc": ["target_checks", 0, "unknown_field"]}]
    assert "secret" not in response.record_error and "private-text" not in response.record_error
    assert validate_record(None, catalog, {"app.py"}, response.record_error).status == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["keep", "drop"])
async def test_string_counterevidence_requires_human_without_fabricated_anchor(fake_chat, decision):
    fake_chat.responses = [AIMessage(content=json.dumps({
        "issue_id": "i", "decision": decision, "reason": "反证需要核实",
        "contradicting_evidence": ["return something_else"], "adjusted_severity": "low",
    }))]
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")
    from app.models.review import IssueMetrics
    batch = await IssueVerifierService(provider, enabled=True, fail_mode="needs_human",
                                       max_calls_per_unit=1).verify_issues(
        [_issue("i", severity="high")], [_unit()], _state(_unit()), IssueMetrics(),
    )
    assert batch.issues[0].status == "needs_human"
    assert not batch.issues[0].auto_fix_eligible
    assert batch.issues[0].unresolved_reason == "verifier_needs_human"
    result = batch.verifications[0]
    assert result.decision == "needs_human"
    assert result.contradicting_evidence == []
    assert result.adjusted_severity is None
    assert "verifier_unstructured_counterevidence" in result.reason


def test_counterevidence_objects_remain_strict():
    raw = {"issue_id": "i", "decision": "drop", "reason": "有反证",
           "contradicting_evidence": [{"file_path": "app.py", "existing_code": "return value"}]}
    result = IssueVerification.model_validate(OpenAICompatibleProvider._normalize_issue_verification(raw))
    assert result.decision == "drop" and result.contradicting_evidence[0].file_path == "app.py"
    raw["contradicting_evidence"][0]["arbitrary_field"] = "unsafe"
    raw["contradicting_evidence"].append("unstructured")
    with pytest.raises(ValidationError):
        IssueVerification.model_validate(OpenAICompatibleProvider._normalize_issue_verification(raw))


def test_all_coordination_operations_use_their_actual_prompt_builder():
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")
    request = IssueVerificationRequest(issue=_issue("i"), primary_evidence=_issue("i").primary_evidence,
                                       unit_diff="return wrong", budget=IssueVerificationBudget(remaining_calls=1))
    samples = {
        "coordinate_cross_units": (({}, None), provider._build_coordination_prompt({})),
        "decide": (({}, None), provider._build_decision_prompt({})),
        "review_unit": ((_sample_pr(), [], "", None, {}),
                        provider._build_unit_review_prompt(_sample_pr(), [], "", {})),
        "verify_issue": ((request, None), provider._build_issue_verification_prompt(request)),
    }
    for name, (args, prompt) in samples.items():
        reservation = estimate_request(provider, name, args, 1200)
        assert reservation["input_chars"] == len(prompt) + 512
        assert reservation["estimate_source"] == "provider_prompt"
    prompt = samples["coordinate_cross_units"][1]
    assert "至少两个不同且已知的 Unit ID" in prompt
    assert "followups 非空时 decision 必须为 required" in prompt
