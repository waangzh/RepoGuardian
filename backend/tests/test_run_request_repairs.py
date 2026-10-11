import asyncio

import pytest
from langchain_core.messages import AIMessage

from app.agents.providers import LLMProviderError, OpenAICompatibleProvider
from app.core.config import settings
from app.models.review import ExecutionBudget, ReviewTask
from app.services.coordination_runtime import CoordinationRuntime
from app.services.cross_unit_coordination import SharedBudgetProvider
from app.services.review_run_budget import ReviewRunBudget, RunBudgetError, activate_run_budget
from test_phase6a_persistence import persistence as persistence
from app.services.model_pricing import estimate_cost_microusd


async def test_unknown_amount_protected_until_reconciled():
    budget = ReviewRunBudget("run", max_tokens=1000, max_calls=3, max_cost_microusd=100)
    attempt = await budget.reserve(request_id="a", operation="review_step", estimated_tokens=10,
                                   estimated_cost_microusd=80)
    await budget.mark_dispatching(attempt.attempt_id)
    await budget.settle(attempt.attempt_id, actual_tokens=None)
    with pytest.raises(RunBudgetError, match="cost_rejected"):
        await budget.reserve(request_id="b", operation="review_step", estimated_tokens=10,
                             estimated_cost_microusd=80)
    await budget.reconcile_unknown(attempt.attempt_id, actual_tokens=5, cost_microusd=20)
    await budget.reserve(request_id="b", operation="review_step", estimated_tokens=10,
                         estimated_cost_microusd=80)
    assert budget.cost_reserved_microusd + budget.cost_settled_microusd == 100


@pytest.mark.parametrize("tokens", [None, 5])
async def test_late_amount_settles_independently_and_once(tokens):
    budget = ReviewRunBudget("run", max_tokens=100, max_calls=2, max_cost_microusd=100)
    attempt = await budget.reserve(request_id="a", operation="review_step", estimated_tokens=10,
                                   estimated_cost_microusd=80)
    await budget.settle(attempt.attempt_id, actual_tokens=tokens)
    assert budget.cost_reserved_microusd == 80
    await budget.reconcile_unknown(attempt.attempt_id, actual_tokens=tokens, cost_microusd=20)
    await budget.reconcile_unknown(attempt.attempt_id, actual_tokens=tokens, cost_microusd=20)
    assert budget.cost_reserved_microusd == 0 and budget.cost_settled_microusd == 20


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "bad"])
def test_invalid_price_is_unknown(value):
    import json
    assert estimate_cost_microusd(json.dumps({"p:m": {"input": value, "output": 1}}),
        provider="p", model="m", input_tokens=10, output_tokens=10) is None


@pytest.mark.parametrize("price,limit,allowed", [(None, 1000, False), (3, 1, False), (3, 1000, True)])
async def test_real_provider_cost_admission(price, limit, allowed, monkeypatch):
    provider = OpenAICompatibleProvider("key", "https://example.test/v1", "fixture", request_attempts=1)
    import json
    pricing = {} if price is None else {f"{provider._provider_name}:fixture": {"input": 2, "output": price}}
    monkeypatch.setattr(settings, "repoguardian_model_pricing_json", json.dumps(pricing))
    sent = []
    class Chat:
        async def ainvoke(self, messages):
            sent.append(messages)
            return AIMessage(content="{}", usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    provider._build_chat_model = lambda *args: Chat()
    budget = ReviewRunBudget("run", max_tokens=10000, max_calls=3, max_cost_microusd=limit)
    with activate_run_budget(budget):
        call = provider._request_json_content("input", "fixture", "review_step", "system", 20)
        if allowed:
            await call
            assert budget.cost_settled_microusd == 35
            assert budget.cost_reserved_microusd == 0
        else:
            with pytest.raises(LLMProviderError, match="run_budget_(price_unavailable|cost_rejected)"):
                await call
    assert bool(sent) == allowed
    assert budget.calls_consumed == int(allowed)


@pytest.mark.parametrize("cancel", [False, True])
async def test_unified_coordination_preserves_attempts_and_replay(persistence, cancel):
    repository, _, _ = persistence
    repository.create_task(ReviewTask(id="run", pr_url="https://github.com/a/b/pull/1"))
    provider = OpenAICompatibleProvider("key", "https://example.test/v1", "fixture", request_attempts=1)
    entered = asyncio.Event()
    sent = []
    class Chat:
        async def ainvoke(self, messages):
            sent.append(messages)
            entered.set()
            if cancel:
                await asyncio.Event().wait()
            return AIMessage(content='{"decision":"uncertain","reason":"需补查"}',
                usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    provider._build_chat_model = lambda *args: Chat()
    budget = ReviewRunBudget("run", max_tokens=100000, max_calls=3)
    runtime = CoordinationRuntime("run", "a" * 64, ExecutionBudget(max_model_calls=0), repository)
    wrapper = SharedBudgetProvider(provider, ExecutionBudget(max_model_calls=0))
    wrapper.runtime = runtime
    await runtime.load()
    with activate_run_budget(budget):
        running = asyncio.create_task(wrapper.coordinate_cross_units({}, None))
        if cancel:
            await asyncio.wait_for(entered.wait(), 5)
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
        else:
            await running
        restarted = CoordinationRuntime("run", "a" * 64, ExecutionBudget(max_model_calls=0), repository)
        await restarted.load()
        if cancel:
            with pytest.raises(LLMProviderError, match="unknown"):
                await restarted.call(provider, "coordinate_cross_units", ({}, None), 4096)
        else:
            await restarted.call(provider, "coordinate_cross_units", ({}, None), 4096)
        assert restarted.metrics().unknown_calls == int(cancel)
        assert restarted.metrics().model_calls == 1
        assert len(next(iter(restarted.data["calls"].values()))["attempt_ids"]) == 1
    assert len(sent) == budget.calls_consumed == 1
