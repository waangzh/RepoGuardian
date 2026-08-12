from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage

from app.agents.providers import OpenAICompatibleProvider
from app.models.review import ModelUsage, ReviewUnitComplexity
from app.services.model_pricing import calculate_cost_microusd
from app.services.model_usage import summarize_model_usage


class _FakeChat:
    def __init__(self, response: AIMessage) -> None:
        self.response = response

    async def ainvoke(self, _messages: object) -> AIMessage:
        return self.response


@pytest.mark.asyncio
async def test_provider_returns_actual_usage_metadata_and_raw_response_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = AIMessage(
        content='{"action":"finish_report","reason":"done"}',
        usage_metadata={
            "input_tokens": 120,
            "output_tokens": 30,
            "total_tokens": 150,
            "input_token_details": {"cache_read": 40},
            "output_token_details": {"reasoning": 10},
        },
        response_metadata={"model_name": "actual-model", "finish_reason": "stop"},
    )
    provider = OpenAICompatibleProvider("key", "https://example.test/v1", "default")
    monkeypatch.setattr(provider, "_build_chat_model", lambda *_: _FakeChat(response))

    result = await provider.decide({}, None)

    assert result.value.action == "finish_report"
    assert result.usage.actual_input_tokens == 120
    assert result.usage.actual_output_tokens == 30
    assert result.usage.actual_total_tokens == 150
    assert result.usage.cached_input_tokens == 40
    assert result.usage.reasoning_output_tokens == 10
    assert result.usage.model == "actual-model"
    assert result.usage.response_metadata["finish_reason"] == "stop"
    assert result.usage.usage_available is True


@pytest.mark.asyncio
async def test_provider_records_successful_response_with_missing_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAICompatibleProvider("key", "https://example.test/v1", "model")
    response = AIMessage(content='{"action":"finish_report","reason":"done"}')
    monkeypatch.setattr(provider, "_build_chat_model", lambda *_: _FakeChat(response))

    result = await provider.decide({}, None)

    assert result.usage.usage_available is False
    assert result.usage.accounting_source == "missing"
    assert result.usage.actual_total_tokens is None
    assert result.usage.estimated_input_tokens > 0


def test_usage_summary_reports_percentiles_missing_rate_and_estimation_delta() -> None:
    usages = [
        _usage("decide", 100, 20, estimate=600, complexity=ReviewUnitComplexity.small),
        _usage("decide", 200, 40, estimate=600, complexity=ReviewUnitComplexity.medium),
        _usage("diagnosis", 300, 60, estimate=4_096, complexity=ReviewUnitComplexity.large),
        ModelUsage(
            provider="openai",
            model="model",
            operation="diagnosis",
            latency_ms=400,
            usage_available=False,
        ),
    ]

    summary = summarize_model_usage(usages)

    assert summary.overall.calls == 4
    assert summary.overall.usage_available_calls == 3
    assert summary.overall.usage_missing_calls == 1
    assert summary.overall.usage_coverage_rate == 0.75
    assert summary.overall.input_tokens_p50 == 200
    assert summary.overall.input_tokens_p95 == 300
    assert summary.overall.output_tokens_p50 == 40
    assert summary.overall.output_tokens_p95 == 60
    assert summary.overall.accounted_tokens_estimate == 5_296
    assert summary.overall.estimation_delta_tokens == 4_576
    by_complexity = {item.key: item.stats for item in summary.by_unit_complexity}
    assert by_complexity["large"].actual_total_tokens == 360
    assert by_complexity["task"].usage_missing_calls == 1


def test_pricing_uses_cached_rate_without_double_counting_tokens() -> None:
    pricing = json.dumps({
        "openai:model": {"input": 2, "output": 8, "cached_input": 0.5}
    })

    cost = calculate_cost_microusd(
        pricing,
        provider="openai",
        model="model",
        input_tokens=1_000,
        output_tokens=100,
        cached_input_tokens=400,
    )

    assert cost == 2_200
    assert calculate_cost_microusd(
        "{}",
        provider="openai",
        model="model",
        input_tokens=1_000,
        output_tokens=100,
        cached_input_tokens=0,
    ) is None


def _usage(
    operation: str,
    input_tokens: int,
    output_tokens: int,
    *,
    estimate: int,
    complexity: ReviewUnitComplexity,
) -> ModelUsage:
    return ModelUsage(
        provider="openai",
        model="model",
        operation=operation,
        unit_complexity=complexity,
        accounted_tokens_estimate=estimate,
        actual_input_tokens=input_tokens,
        actual_output_tokens=output_tokens,
        actual_total_tokens=input_tokens + output_tokens,
        latency_ms=input_tokens,
        usage_available=True,
        accounting_source="actual",
    )
