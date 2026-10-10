import pytest
from langchain_core.messages import AIMessage

from app.agents.providers import OpenAICompatibleProvider
from app.models.review import PullRequestInfo, PullRequestRef
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


@pytest.mark.asyncio
async def test_review_step_returns_one_controlled_next_need():
    provider = OpenAICompatibleProvider("key", "https://example.test/v1", "fixture", request_attempts=1)
    response = {
        "issues": [],
        "review_record": {"change_summary": "检查变更", "target_checks": [],
                           "hypothesis_checks": [], "contract_dependencies": [],
                           "unresolved_questions": [], "question_updates": [],
                           "file_change_checks": []},
        "next_need": {"action": "task_done", "reason": "当前范围已检查"},
    }
    class Chat:
        async def ainvoke(self, messages):
            return AIMessage(content=__import__("json").dumps(response),
                             usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    provider._build_chat_model = lambda model, max_tokens: Chat()
    pr = PullRequestInfo(owner="o", repo="r", number=1, title="t", html_url="https://x/pr/1",
                         clone_url="https://x/r.git",
                         base=PullRequestRef(sha="b", ref="main", repo_clone_url="https://x/r.git"),
                         head=PullRequestRef(sha="h", ref="head", repo_clone_url="https://x/r.git"))
    result = await provider.review_step(pr, [], "", "fixture", {
        "snapshot": {"input_version": "unit-evidence-chunks-v3"},
        "input_protocol": "canonical-evidence-v3", "readonly_context": [], "evidence": [],
    })
    assert result.value.next_need.action.value == "task_done"
