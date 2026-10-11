import json

import pytest
from langchain_core.messages import AIMessage

from app.agents.providers import LLMProvider, LLMProviderError, OpenAICompatibleProvider
from app.models.review import AgentAction, ExecutionBudget, ModelCallResult, ModelUsage, ReviewUnit, UnitReviewPlan, UnitReviewRecord, UnitReviewResponse
from app.review.input_protocol import CANONICAL_UNIT_INPUT_PROTOCOL
from app.services.model_request_budgeter import RequestAdmissionError, UnitRequestLedger, request_budget_reserver
from app.services.review_unit_executor import ReviewUnitExecutor
from test_context_request_admission import chat as chat
from test_unit_review_summary import _file, _state


@pytest.mark.asyncio
async def test_user_example_rejects_optional_decision_without_spending_diagnosis_allowance():
    budget = ExecutionBudget(max_token_usage=12000)
    unprotected = UnitRequestLedger(budget)
    await unprotected.reserve({"reserved_tokens": 9394})
    unprotected.settle(ModelUsage(provider="p", model="m", operation="decide", latency_ms=0, actual_total_tokens=2000))
    assert unprotected.budget.max_token_usage - unprotected.budget.token_usage == 10000
    protected = UnitRequestLedger(budget, holdback={"operation": "diagnosis", "model_calls": 1, "token_usage": 5290})
    with pytest.raises(RequestAdmissionError, match="unit_request_budget_exhausted"):
        await protected.reserve({"reserved_tokens": 9394})
    assert protected.budget == budget
    assert protected.rejection["available_tokens"] == 6710
    final = UnitRequestLedger(protected.budget)
    await final.reserve({"reserved_tokens": 5290})
    assert final.budget.token_usage == 5290 and final.budget.model_calls == 1


@pytest.mark.asyncio
async def test_success_releases_unused_reservation_without_consuming_holdback():
    ledger = UnitRequestLedger(ExecutionBudget(max_token_usage=12000),
                               holdback={"model_calls": 1, "token_usage": 5290})
    await ledger.reserve({"reserved_tokens": 6000})
    usage = ModelUsage(provider="p", model="m", operation="decide", latency_ms=0, actual_total_tokens=2000)
    ledger.settle(usage)
    ledger.settle(usage)
    assert ledger.budget.token_usage == 2000
    final = UnitRequestLedger(ledger.budget)
    await final.reserve({"reserved_tokens": 5290})
    assert final.budget.token_usage == 7290


@pytest.mark.asyncio
async def test_retry_cannot_take_protected_call_or_tokens(chat):
    provider = OpenAICompatibleProvider("key", "https://example.test/v1", "unknown-model", retry_backoff_seconds=0)
    request = provider._prepare_json_request("human", None, "decide", "system", 1200)
    amount = request.estimate(provider._request_profile)["reserved_tokens"]
    held = 5290
    ledger = UnitRequestLedger(ExecutionBudget(max_model_calls=4, max_token_usage=amount + held),
                               holdback={"model_calls": 1, "token_usage": held})
    chat.responses = [TimeoutError(), AIMessage(content="{}")]
    with ledger.activate(), pytest.raises(LLMProviderError, match="unit_request_budget_exhausted"):
        await provider._request_json_content("human", None, "decide", "system", 1200)
    assert len(chat.sent) == 1 and ledger.budget.token_usage == amount
    assert ledger.rejection["transport_retry"] is True
    final = UnitRequestLedger(ledger.budget)
    await final.reserve({"reserved_tokens": held})
    assert final.budget.model_calls == 2


@pytest.mark.asyncio
async def test_protected_call_limit_is_independent_of_token_limit():
    ledger = UnitRequestLedger(ExecutionBudget(max_model_calls=1, max_token_usage=12000),
                               holdback={"model_calls": 1, "token_usage": 5290})
    with pytest.raises(RequestAdmissionError):
        await ledger.reserve({"reserved_tokens": 1})
    assert ledger.budget.model_calls == ledger.budget.token_usage == 0


class BoundedProvider(LLMProvider):
    unit_input_protocol = CANONICAL_UNIT_INPUT_PROTOCOL
    supports_request_admission = True

    def __init__(self, *, growing_plan=False, diagnosis_fails=False):
        self.sent = []
        self.diagnoses = []
        self.growing_plan = growing_plan
        self.diagnosis_fails = diagnosis_fails

    def unit_diagnosis_admission(self, pr, files, diff, model, record_input):
        amount = 20000 if record_input["hypotheses"] else 5290
        return {"reserved_tokens": amount, "count_method": "deterministic_fixture"}

    async def reserve(self, operation, amount):
        await request_budget_reserver.get()({"reserved_tokens": amount})
        self.sent.append(operation)

    async def plan_review_unit(self, state, model):
        await self.reserve("plan", 1000 if self.growing_plan else 9394)
        return UnitReviewPlan(change_summary="规划补充风险", review_objectives=["审查变更"], risk_hypotheses=[{
            "id": "h", "category": "correctness", "priority": "high", "description": "核验调用方",
            "completion_criteria": "核对契约"}], initial_action=AgentAction(action="code_search", reason="查找调用方",
                tool_args={"request": {"query": "value", "relation": "text", "max_results": 1}}))

    async def decide(self, state, model):
        await self.reserve("decide", 9394)
        return AgentAction(action="report_issue", reason="检查")

    async def review_unit(self, pr, files, diff, model, record_input):
        await self.reserve("diagnosis", 5290)
        self.diagnoses.append(record_input)
        if self.diagnosis_fails:
            raise LLMProviderError("diagnosis_transport_failed")
        record = UnitReviewRecord(change_summary="已检查完整证据", target_checks=[{
            "target": record_input["targets"][0], "status": "checked", "reason": "核对变更",
            "evidence_ids": [record_input["evidence"][0]["id"]]}])
        return ModelCallResult(UnitReviewResponse(review_record=record), ModelUsage(
            provider="p", model="m", operation="diagnosis", latency_ms=0, actual_total_tokens=2000))

    async def review(self, *args):
        raise AssertionError("canonical provider must use review_unit")

    async def generate_patch(self, *args):
        raise AssertionError("read-only Unit")


def executor(provider, *, calls=5, tokens=12000):
    runner = ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=10)
    runner._budget_for = lambda unit: ExecutionBudget(max_model_calls=calls, max_token_usage=tokens,
                                                      max_diagnosis_attempts=1)
    return runner


def unit():
    return ReviewUnit(id="u", fingerprint="f", primary_files=["a.py"], complexity="medium",
                      risk_tags=["public_api"],
                      estimated_tokens=10, grouping_reason="risk")


@pytest.mark.asyncio
async def test_optional_plan_and_decision_are_skipped_so_diagnosis_can_finish():
    provider = BoundedProvider()
    result = await executor(provider).execute_unit(unit(), _state())
    assert result.status == "completed", result.error
    assert result.review_summary.latest_attempt_status == "reported"
    assert provider.sent == ["diagnosis"]
    assert result.execution_budget.token_usage == 2000 and result.execution_budget.model_calls == 1
    assert result.plan_status == "skipped" and result.plan_skip_reason == "diagnosis_budget_protected"
    assert any("保护最终诊断预算" in event.reason for event in result.messages)


@pytest.mark.asyncio
async def test_single_remaining_call_is_used_for_diagnosis():
    provider = BoundedProvider()
    result = await executor(provider, calls=1).execute_unit(unit(), _state())
    assert result.status == "completed", result.error
    assert provider.sent == ["diagnosis"] and result.execution_budget.model_calls == 1


@pytest.mark.asyncio
async def test_growing_optional_plan_is_explicitly_degraded_without_losing_evidence():
    provider = BoundedProvider(growing_plan=True)
    runner = executor(provider)
    state = _state()
    # Use a plan that goes straight to diagnosis; avoid target repository execution.
    async def planning(value, model):
        await provider.reserve("plan", 1000)
        return UnitReviewPlan(change_summary="新风险", review_objectives=["审查新风险"], risk_hypotheses=[{
            "id": "h", "category": "correctness", "priority": "high", "description": "检查契约",
            "completion_criteria": "核对来源"}], initial_action=AgentAction(action="report_issue", reason="核验"))
    provider.plan_review_unit = planning
    result = await runner.execute_unit(unit(), state)
    assert result.status == "completed", result.error
    assert provider.sent == ["plan", "diagnosis"]
    assert result.plan_error is None, result.plan_error
    actual = provider.diagnoses[0]
    assert actual["input_degradation"]["evidence_coverage"] == "complete"
    assert actual["hypotheses"] == [] and actual["working_memory"] == {}
    assert actual["evidence"][0]["content"] and actual["pr_intent"]["trust"] == "unverified"
    assert result.plan is None and result.plan_skip_reason == "diagnosis_background_budget_degraded"
    assert result.review_summary.status == "unknown" and result.review_summary.latest_attempt_status == "unknown"
    assert result.review_summary.last_valid_record is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["transport", "required_too_large", "no_calls"])
async def test_failed_or_unaffordable_diagnosis_stays_failed_and_unknown(failure):
    provider = BoundedProvider(diagnosis_fails=failure == "transport")
    runner = executor(provider, tokens=5000 if failure == "required_too_large" else 12000,
                      calls=0 if failure == "no_calls" else 5)
    result = await runner.execute_unit(unit(), _state())
    assert result.status == "failed" and result.review_summary.status == "unknown"
    assert result.error
    if failure != "transport":
        assert provider.sent == [] and result.execution_budget.token_usage == 0
    else:
        assert provider.sent == ["diagnosis"] and result.execution_budget.token_usage == 5290


@pytest.mark.asyncio
async def test_diagnosis_preflight_matches_actual_prepared_request(chat):
    from app.services.unit_review_summary import build_record_input
    from app.models.review import PullRequestInfo

    state = _state()
    pr = PullRequestInfo.model_validate(state["pr_info"])
    provider = OpenAICompatibleProvider("key", "https://example.test/v1", "unknown-model")
    record_input = build_record_input([_file()], [], None, "head-a")
    estimate = provider.unit_diagnosis_admission(pr, [_file()], "", None, record_input)
    assert chat.sent == []
    chat.responses = [AIMessage(content=json.dumps({"issues": [], "review_record": {"change_summary": "核验"}}))]
    result = await provider.review_unit(pr, [_file()], "", None, record_input)
    assert result.usage.response_metadata["request_admission"] == estimate


def test_followup_cannot_discard_its_required_question_to_fit_budget():
    provider = BoundedProvider()
    runner = executor(provider)
    current_unit = unit()
    scope = runner.planner.build_scope(current_unit, repository_files={"a.py"})
    state = {"parent_state": {**_state(), "cross_unit_followup": {"question": "组合契约"}},
             "unit": current_unit, "unit_files": [_file()],
             "scope": scope, "unit_diff": "+return value", "context": [],
             "budget": ExecutionBudget(max_token_usage=12000),
             "unit_plan": UnitReviewPlan(change_summary="组合问题", review_objectives=["组合契约"],
                 risk_hypotheses=[{"id": "h", "category": "correctness", "priority": "high",
                                   "description": "核验两侧", "completion_criteria": "核对完整证据"}],
                 initial_action=AgentAction(action="report_issue", reason="核验"))}
    args, degradation = runner._select_diagnosis_input(state)
    assert degradation is None and args[-1]["hypotheses"][0]["id"] == "h"
    assert args[-1]["targets"] == ["组合契约"]


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["execute", "report"])
async def test_full_and_core_capacity_rejection_persists_one_diagnosis_admission_attempt(chat, entry):
    from app.services.model_request_budgeter import ModelRequestProfile

    provider = OpenAICompatibleProvider("key", "https://example.test/v1", "unknown-model",
        request_profile=ModelRequestProfile(max_input_tokens=1))
    runner = ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=10)
    if entry == "execute":
        result = await runner.execute_unit(unit(), _state())
        budget, summary, error = result.execution_budget, result.review_summary, result.error
        assert result.status == "failed"
    else:
        current = unit()
        state = {"unit": current, "unit_files": [_file()], "context": [], "unit_diff": "+return value",
            "parent_state": _state(), "budget": ExecutionBudget(),
            "scope": runner.planner.build_scope(current, repository_files={"a.py"})}
        result = await runner._report_issue_node(state)
        budget, summary, error = result["budget"], result["review_summary"], result["error"]
    assert budget.diagnosis_attempts == 1
    assert budget.model_calls == budget.token_usage == 0
    assert summary.status == "unknown" and summary.latest_attempt_status == "failed"
    assert "model_input_limit_exceeded" in error and chat.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("record_status", ["missing", "invalid"])
async def test_background_degradation_does_not_hide_missing_or_invalid_record(record_status):
    provider = BoundedProvider()
    runner = executor(provider)
    current = unit()
    state = {"unit": current, "unit_files": [_file()], "context": [], "unit_diff": "+return value",
        "parent_state": _state(), "budget": ExecutionBudget(max_token_usage=12000),
        "scope": runner.planner.build_scope(current, repository_files={"a.py"}),
        "unit_plan": UnitReviewPlan(change_summary="风险目标", review_objectives=["业务目标"],
            risk_hypotheses=[{"id": "h", "category": "correctness", "priority": "high",
                              "description": "核验契约", "completion_criteria": "核对证据"}],
            initial_action=AgentAction(action="report_issue", reason="核验"))}
    async def response(*args):
        await provider.reserve("diagnosis", 5290)
        record = None if record_status == "missing" else UnitReviewRecord(change_summary="无效引用",
            target_checks=[{"target": args[-1]["targets"][0], "status": "checked", "reason": "核验",
                            "evidence_ids": ["unknown-evidence"]}])
        return UnitReviewResponse(review_record=record)
    provider.review_unit = response
    result = await runner._report_issue_node(state)
    assert result["plan_skip_reason"] == "diagnosis_background_budget_degraded"
    assert result["review_summary"].status == "unknown"
    assert result["review_summary"].latest_attempt_status == record_status
    assert "diagnosis_background_budget_degraded" != result["review_summary"].reason
