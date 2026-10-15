import json

import pytest
from langchain_core.messages import AIMessage

from app.agents.providers import LLMProviderError, OpenAICompatibleProvider
from app.models.review import ExecutionBudget
from app.services.coordination_runtime import CoordinationRuntime
from app.services.cross_unit_coordination import CrossUnitCoordinationService
from test_cross_unit_coordination import plan, state
from test_provider import fake_chat as fake_chat


def response(payload, *, decision="uncertain", tokens=6668):
    raw = plan(payload).model_dump(mode="json", include={"decision", "reason", "followups"})
    raw["decision"] = decision
    return AIMessage(content=json.dumps(raw, ensure_ascii=False), usage_metadata={
        "input_tokens": tokens - 100, "output_tokens": 100, "total_tokens": tokens} if tokens is not None else None)


@pytest.mark.asyncio
async def test_provider_conflict_keeps_usage_and_repair_has_distinct_request_and_accounting(fake_chat):
    payload = CrossUnitCoordinationService.catalog(state())
    fake_chat.responses = [response(payload), response(payload, decision="required", tokens=7000)]
    provider = OpenAICompatibleProvider("key", "https://example.test/v1", "model")
    runtime = CoordinationRuntime("task", "fingerprint", ExecutionBudget(max_model_calls=10, max_token_usage=200000))
    with pytest.raises(LLMProviderError) as caught:
        await runtime.call(provider, "coordinate_cross_units", (payload, None), 4096)
    error = caught.value
    assert error.coordination_error_code == "decision_followups_conflict"
    assert error.usage.actual_total_tokens == 6668
    assert error.output_schema_errors[0]["loc"] == ["decision", "followups"]
    feedback = {"attempt": 1, "errors": error.output_schema_errors}
    prepared = provider.prepare_coordination_request(payload, None, feedback)
    result = await runtime.call(provider, "repair_cross_unit_plan", (payload, None, feedback), 4096)
    assert result.decision == "required"
    assert result.usage.operation == "cross_unit_coordination_repair"
    assert prepared.prompt == fake_chat.instances[1].messages[1].content
    assert result.usage.response_metadata["request_admission"]["request_hash"] == prepared.estimate(
        provider.resolve_request_profile(None).profile)["request_hash"]
    assert runtime.budget.model_calls == 2
    assert runtime.budget.token_usage == 13668
    assert sorted(item["status"] for item in runtime.data["calls"].values()) == ["completed", "failed"]
    with pytest.raises(LLMProviderError) as restored:
        await runtime.call(provider, "coordinate_cross_units", (payload, None), 4096)
    assert restored.value.output_schema_errors == error.output_schema_errors
    assert restored.value.usage.id == error.usage.id
    await runtime.call(provider, "repair_cross_unit_plan", (payload, None, feedback), 4096)
    assert len(fake_chat.instances) == 2
    assert runtime.budget.token_usage == 13668


def test_prompt_uses_three_mutually_exclusive_examples_without_reinterpreting_risk():
    prompt = OpenAICompatibleProvider._build_coordination_prompt({})
    examples = [json.loads(line) for line in prompt.splitlines() if line.startswith('{"decision"')]
    assert {item["decision"] for item in examples} == {"required", "uncertain", "skip"}
    assert all(not item["followups"] for item in examples if item["decision"] != "required")
    assert next(item["followups"] for item in examples if item["decision"] == "required")
    assert "不表示风险已经确认" in prompt
