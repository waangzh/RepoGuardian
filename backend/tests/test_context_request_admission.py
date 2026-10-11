import asyncio
import json

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from app.agents.providers import LLMProviderError, OpenAICompatibleProvider
from app.core.config import Settings
from app.models.review import ExecutionBudget, ModelUsage
from app.services.context_assembler import RequiredInputTooLarge, assemble_context
from app.services.model_request_budgeter import (
    ModelRequestProfile, PreparedModelRequest, RequestAdmissionError, UnitRequestLedger,
)
from app.services.review_unit_executor import ReviewUnitExecutor
from app.services.coordination_runtime import CoordinationRuntime


@pytest.mark.parametrize("stage,limit", [("decision", 50_000), ("plan", 80_000)])
def test_large_catalog_preserves_valid_json_and_all_required_unit_input(stage, limit):
    state = {
        "unit_agent": True, "review_unit": {"id": "UNIT_SENTINEL", "primary_files": ["a.py"]},
        "unit_diff": "DIFF_SENTINEL\n" + "+中文\\\"\n" * 3000,
        "unit_plan": {"change_summary": "PLAN_SENTINEL"},
        "execution_budget": {"max_token_usage": 10000}, "changed_files": [],
        "file_index": [{"path": f"src/{i}/" + "x" * 100} for i in range(2000)],
        "symbol_index": [{"file": "a.py", "symbol": "x" * 100} for _ in range(3000)],
        "context_snippets": [{"file": "a.py", "start_line": i + 1,
                              "content": "完整片段" * 1000} for i in range(12)],
    }
    builder = (OpenAICompatibleProvider._build_decision_prompt if stage == "decision"
               else OpenAICompatibleProvider._build_unit_plan_prompt)
    marker = "Current state JSON:\n" if stage == "decision" else "Bounded Unit input JSON:\n"
    serialized = builder(state).split(marker)[1]
    payload = json.loads(serialized)
    assert len(serialized) <= limit
    assert payload["review_unit"] == state["review_unit"]
    assert payload["diff_evidence"] == {"format": "unbound_diff_text", "text": state["unit_diff"]}
    assert "unit_diff" not in payload and "changed_files" not in payload
    if stage == "decision":
        assert payload["unit_plan"] == state["unit_plan"]
        assert payload["execution_budget"] == state["execution_budget"]
        for item in payload.get("observed_context", {}).get("snippets", []):
            assert item["content"] == "完整片段" * 1000
    assert payload["input_manifest"]["required_coverage"] == "complete"
    assert any(payload["input_manifest"]["omitted"].values())


@pytest.mark.parametrize("stage", ["decision", "plan"])
def test_long_core_diff_is_rejected_without_prefix_only_coverage(stage):
    state = {"unit_agent": True, "review_unit": {"id": "u"}, "unit_diff": "x" * 100_000}
    builder = (OpenAICompatibleProvider._build_decision_prompt if stage == "decision"
               else OpenAICompatibleProvider._build_unit_plan_prompt)
    with pytest.raises(LLMProviderError, match="required_input_too_large"):
        builder(state)


def test_assembler_selects_complete_items_and_reports_omissions():
    items = [{"content": "不可切断的内容" * 40, "id": i} for i in range(20)]
    payload = json.loads(assemble_context({"unit_diff": "full", "snippets": items}, {"unit_diff"}, 1500))
    chosen = payload["snippets"]
    assert 0 < len(chosen) < len(items)
    assert chosen == items[:len(chosen)]
    assert payload["input_manifest"]["omitted"]["snippets"] == len(items) - len(chosen)
    with pytest.raises(RequiredInputTooLarge):
        assemble_context({"unit_diff": "x" * 2000}, {"unit_diff"}, 1500)


@pytest.mark.parametrize("text", ["中文" * 100, '"\\\n\t' * 100, "🙂" * 100, "return foo(x);" * 100])
def test_unknown_model_counts_complete_serialized_request_conservatively(text):
    request = PreparedModelRequest("unknown-model", "decide", "SYSTEM_SENTINEL", text, 1200)
    metadata = request.estimate(ModelRequestProfile(context_window=100_000))
    assert metadata["estimated_input_tokens"] > len(text.encode("utf-8"))
    assert metadata["reserved_tokens"] == metadata["estimated_input_tokens"] + 1200
    assert metadata["count_method"].startswith("conservative_utf8_bytes")
    assert metadata["count_is_exact"] is False


def test_input_output_window_and_safety_margin_are_independent():
    request = PreparedModelRequest("unknown-model", "decide", "system", "human", 1200)
    metadata = request.estimate(ModelRequestProfile(context_window=100_000))
    tokens = metadata["estimated_input_tokens"]
    request.estimate(ModelRequestProfile(context_window=tokens + 1200 + 512))
    with pytest.raises(RequestAdmissionError, match="model_context_window_exceeded"):
        request.estimate(ModelRequestProfile(context_window=tokens + 1200 + 511))
    request.estimate(ModelRequestProfile(context_window=tokens + 512, input_output_shared=False))
    with pytest.raises(RequestAdmissionError, match="model_input_limit_exceeded"):
        request.estimate(ModelRequestProfile(max_input_tokens=tokens - 1))
    with pytest.raises(RequestAdmissionError, match="model_output_limit_exceeded"):
        request.estimate(ModelRequestProfile(max_output_tokens=1199))


class Chat:
    responses = []
    sent = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def ainvoke(self, messages):
        self.sent.append(messages)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def chat(monkeypatch):
    Chat.sent = []
    Chat.responses = []
    monkeypatch.setattr("app.agents.providers.ChatOpenAI", Chat)
    return Chat


async def call(provider, prompt="human"):
    return await provider._request_json_content(prompt, None, "decide", "system", 1200)


@pytest.mark.asyncio
async def test_window_overflow_prevents_transport_and_unit_charge(chat):
    provider = OpenAICompatibleProvider("key", "https://example.test/v1", "unknown-model",
                                        request_profile=ModelRequestProfile(context_window=2000))
    ledger = UnitRequestLedger(ExecutionBudget())
    with ledger.activate(), pytest.raises(LLMProviderError, match="model_context_window_exceeded"):
        await call(provider, "中文" * 1000)
    assert chat.sent == []
    assert ledger.budget.token_usage == 0
    assert ledger.budget.model_calls == 0


@pytest.mark.asyncio
async def test_retry_uses_same_request_and_every_attempt_reserves(chat):
    provider = OpenAICompatibleProvider("key", "https://example.test/v1", "unknown-model",
                                        retry_backoff_seconds=0)
    chat.responses = [TimeoutError(), AIMessage(content="{}")]
    ledger = UnitRequestLedger(ExecutionBudget(max_model_calls=2, max_token_usage=10000))
    with ledger.activate():
        result = await call(provider)
    amount = result.usage.response_metadata["request_admission"]["reserved_tokens"]
    assert ledger.budget.model_calls == 2
    assert ledger.budget.token_usage == 2 * amount
    assert chat.sent[0] is chat.sent[1]
    assert result.usage.estimated_input_tokens == amount - 1200
    assert result.usage.usage_available is False
    assert result.usage.response_metadata["request_attempts"] == 2


@pytest.mark.asyncio
async def test_retry_and_parallel_reservations_do_not_exceed_budget(chat):
    provider = OpenAICompatibleProvider("key", "https://example.test/v1", "unknown-model",
                                        retry_backoff_seconds=0)
    chat.responses = [TimeoutError()]
    ledger = UnitRequestLedger(ExecutionBudget(max_model_calls=1))
    with ledger.activate(), pytest.raises(LLMProviderError, match="unit_request_budget_exhausted"):
        await call(provider)
    assert len(chat.sent) == 1
    assert ledger.budget.model_calls == 1
    assert ledger.budget.token_usage > 1200
    shared = UnitRequestLedger(ExecutionBudget(max_model_calls=2, max_token_usage=2000))
    results = await asyncio.gather(*[shared.reserve({"reserved_tokens": 1500}) for _ in range(2)],
                                   return_exceptions=True)
    assert sum(isinstance(item, RequestAdmissionError) for item in results) == 1
    assert shared.budget.token_usage == 1500


@pytest.mark.asyncio
async def test_actual_usage_corrects_ledger_once_and_failure_keeps_reserved_tokens(chat):
    provider = OpenAICompatibleProvider("key", "https://example.test/v1", "unknown-model",
                                        request_attempts=1)
    executor = ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=30)
    chat.responses = [AIMessage(content="{}", usage_metadata={
        "input_tokens": 2000, "output_tokens": 1000, "total_tokens": 3000})]
    result, budget = await executor._call_unit_model(ExecutionBudget(), {}, 1200, lambda: call(provider))
    assert budget.token_usage == 3000
    assert result.usage.accounted_tokens_estimate < 3000
    ledger = UnitRequestLedger(ExecutionBudget(max_token_usage=1500))
    await ledger.reserve({"reserved_tokens": 1200})
    usage = ModelUsage(provider="p", model="m", operation="decide", latency_ms=0,
                       actual_total_tokens=3000)
    with pytest.raises(ValueError, match="unit_request_budget_overrun"):
        ledger.settle(usage)
    ledger.settle(usage)
    assert ledger.budget.token_usage == 3000
    with pytest.raises(RequestAdmissionError):
        await ledger.reserve({"reserved_tokens": 1})
    chat.responses = [RuntimeError("transport failed")]
    with pytest.raises(LLMProviderError) as captured:
        await executor._call_unit_model(ExecutionBudget(), {}, 1200, lambda: call(provider))
    assert captured.value.execution_budget.model_calls == 1
    assert captured.value.execution_budget.token_usage > 1200
    assert captured.value.usage.usage_available is False


def test_profile_configuration_rejects_invalid_fields_and_supports_model_overrides():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, repoguardian_model_request_profile={"context_window": 0})
    with pytest.raises(ValidationError):
        Settings(_env_file=None, repoguardian_model_request_profile={"unknown_field": 1})
    config = Settings(_env_file=None, repoguardian_model_request_profiles={
        "custom-model": {"context_window": 32000, "max_input_tokens": 16000}})
    assert config.repoguardian_model_request_profiles["custom-model"].max_input_tokens == 16000


@pytest.mark.asyncio
async def test_prepared_coordination_request_protects_holdback_and_cache_is_free(chat):
    provider = OpenAICompatibleProvider("key", "https://example.test/v1", "unknown-model",
                                        retry_backoff_seconds=0)
    runtime = CoordinationRuntime("task", "fingerprint", ExecutionBudget(
        max_model_calls=5, max_token_usage=10000))
    # Force rejection with protected verification allowance before any transport.
    with pytest.raises(LLMProviderError, match="cross_unit_shared_budget_exhausted"):
        await runtime.call(provider, "decide", ({}, None), 1200,
                           holdback={"token_usage": 9999, "phase": "rejected"})
    assert chat.sent == []
    assert runtime.budget.model_calls == 0
    assert runtime.budget.token_usage == 0
    chat.responses = [AIMessage(content='{"action":"task_done","reason":"完成"}',
                               usage_metadata={"input_tokens": 5000, "output_tokens": 1000,
                                               "total_tokens": 6000})]
    first = await runtime.call(provider, "decide", ({}, None), 1200,
                               holdback={"phase": "accepted"})
    assert runtime.budget.model_calls == 1
    assert runtime.budget.token_usage == max(6000, first.usage.response_metadata[
        "request_admission"]["reserved_tokens"])
    budget = runtime.budget
    cached = await runtime.call(provider, "decide", ({}, None), 1200,
                                holdback={"phase": "accepted"})
    assert cached.value == first.value
    assert runtime.budget == budget
    assert len(chat.sent) == 1


@pytest.mark.asyncio
async def test_diagnosis_failure_preserves_budget_and_cannot_be_collected_as_clean(chat):
    from types import SimpleNamespace
    from test_provider import _sample_pr
    from app.models.review import ReviewUnitComplexity, ReviewUnitTerminalReason

    provider = OpenAICompatibleProvider("key", "https://example.test/v1", "unknown-model",
                                        request_attempts=1)
    executor = ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=30)
    state = {
        "budget": ExecutionBudget(max_token_usage=20000),
        "unit": SimpleNamespace(id="u", complexity=ReviewUnitComplexity.small),
        "parent_state": {"pr_info": _sample_pr().model_dump(mode="json")},
        "unit_files": [], "context": [], "unit_diff": "",
        "scope": SimpleNamespace(readable_files=set()),
    }
    chat.responses = [RuntimeError("transport failed")]
    result = await executor._report_issue_node(state)
    assert result["terminal_reason"] == ReviewUnitTerminalReason.provider_error
    assert result["budget"].diagnosis_attempts == 1
    assert result["budget"].model_calls == 1
    assert result["budget"].token_usage > 4096
    assert result["model_usages"][0]["accounted_tokens_estimate"] == result["budget"].token_usage
    collected = await executor._collect_issue_node(result)
    assert collected["next_action"].action == "task_done"
    assert "issue_round_completed" not in collected


@pytest.mark.asyncio
async def test_cancel_keeps_reserved_snapshot_and_restores_budget_hook(chat, monkeypatch):
    from app.services.model_request_budgeter import unit_budget_snapshot, request_budget_reserver

    provider = OpenAICompatibleProvider("key", "https://example.test/v1", "unknown-model")
    executor = ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=30)
    started = asyncio.Event()

    async def pending(messages):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(chat, "ainvoke", staticmethod(pending))
    snapshot = {}
    token = unit_budget_snapshot.set(snapshot)
    try:
        task = asyncio.create_task(executor._call_unit_model(
            ExecutionBudget(), {}, 1200, lambda: call(provider)))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert snapshot["budget"].model_calls == 1
        assert snapshot["budget"].token_usage > 1200
        assert request_budget_reserver.get() is None
    finally:
        unit_budget_snapshot.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("actual_usage", [False, True])
async def test_followup_checks_unit_and_task_budget_and_cached_result_does_not_charge_again(chat, actual_usage):
    from app.services.cross_unit_coordination import SharedBudgetProvider

    provider = OpenAICompatibleProvider("key", "https://example.test/v1", "unknown-model")
    shared = SharedBudgetProvider(provider, ExecutionBudget())
    shared.runtime = CoordinationRuntime("task", "fingerprint", ExecutionBudget(max_token_usage=20000))
    executor = ReviewUnitExecutor(shared, concurrency=1, timeout_seconds=30)
    chat.responses = [AIMessage(content='{"action":"task_done","reason":"完成"}', usage_metadata=(
        {"input_tokens": 9000, "output_tokens": 1000, "total_tokens": 10000} if actual_usage else None))]
    result, budget = await executor._call_unit_model(ExecutionBudget(), {}, 1200,
                                               lambda: shared.decide({}, None))
    assert budget.model_calls == shared.budget.model_calls == 1
    assert budget.token_usage == shared.budget.token_usage > 1200
    assert result.usage.accounted_tokens_estimate > 1200
    _, cached_budget = await executor._call_unit_model(budget, {}, 1200,
                                                      lambda: shared.decide({}, None))
    assert cached_budget == budget
    assert len(chat.sent) == 1
    # A fresh request fitting the Task must still be rejected by an exhausted Unit.
    with pytest.raises(LLMProviderError, match="unit_request_budget_exhausted"):
        await executor._call_unit_model(ExecutionBudget(max_model_calls=0), {}, 1200,
                                         lambda: shared.decide({"unit_diff": "fresh"}, None))
    assert shared.budget.model_calls == 1
    assert len(chat.sent) == 1
