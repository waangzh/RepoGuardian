import pytest
from langchain_core.messages import AIMessage

from app.agents.providers import OpenAICompatibleProvider
from app.services.model_request_budgeter import request_budget_reserver
from app.services.review_run_budget import ReviewRunBudget, activate_run_budget


class _Chat:
    async def ainvoke(self, messages):
        return AIMessage(content="{}", usage_metadata={
            "input_tokens": 12, "output_tokens": 3, "total_tokens": 15,
        })


@pytest.mark.asyncio
async def test_provider_uses_run_budget_as_single_reservation_authority(monkeypatch):
    provider = OpenAICompatibleProvider("key", "https://example.test/v1", "fixture",
                                       request_attempts=1)
    provider._build_chat_model = lambda model, max_tokens: _Chat()
    budget = ReviewRunBudget("run", max_tokens=1000, max_calls=1)
    local_hook_called = False

    async def fail_local(_metadata):
        nonlocal local_hook_called
        local_hook_called = True
        raise AssertionError("Unit ledger must not be a second authority")

    token = request_budget_reserver.set(fail_local)
    try:
        with activate_run_budget(budget):
            await provider._request_json_content(
                prompt="bounded input", model="fixture", operation="review_step",
                system="return json", max_tokens=20,
            )
    finally:
        request_budget_reserver.reset(token)
    assert not local_hook_called
    assert budget.calls_consumed == 1
    assert budget.settled_actual == 15
    assert len(budget.attempts) == 1


@pytest.mark.asyncio
async def test_provider_rejects_before_transport_when_run_budget_is_exhausted(monkeypatch):
    provider = OpenAICompatibleProvider("key", "https://example.test/v1", "fixture",
                                       request_attempts=1)
    called = False

    class Chat:
        async def ainvoke(self, messages):
            nonlocal called
            called = True
            return AIMessage(content="{}")

    provider._build_chat_model = lambda model, max_tokens: Chat()
    budget = ReviewRunBudget("run", max_tokens=1, max_calls=1)
    with activate_run_budget(budget), pytest.raises(Exception, match="run_budget_admission_rejected"):
        await provider._request_json_content(
            prompt="input", model="fixture", operation="verify_issue",
            system="return json", max_tokens=20,
        )
    assert not called and not budget.attempts
