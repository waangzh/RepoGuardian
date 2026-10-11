import asyncio
import json

import pytest
from langchain_core.messages import AIMessage

from app.agents.providers import OpenAICompatibleProvider
from app.models.review import ExecutionBudget, IssueMetrics, ReviewTask
from app.services.coordination_runtime import CoordinationRuntime
from app.services.cross_unit_coordination import SharedBudgetProvider
from app.services.issue_verifier import IssueVerifierService
from app.services.review_run_budget import ReviewRunBudget, activate_run_budget, run_budget_attempt_observer
from test_issue_validation import _issue, _unit, _state
from test_phase6a_persistence import persistence as persistence


def verifier_and_chat():
    provider = OpenAICompatibleProvider("key", "https://example.test/v1", "fixture", request_attempts=1)
    sent = []
    class Chat:
        async def ainvoke(self, messages):
            sent.append(messages)
            return AIMessage(content=json.dumps({"issue_id": "i", "decision": "keep", "reason": "supported"}),
                usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    provider._build_chat_model = lambda *args: Chat()
    wrapper = SharedBudgetProvider(provider, ExecutionBudget())
    wrapper.runtime = CoordinationRuntime("run", "a" * 64, ExecutionBudget())
    verifier = IssueVerifierService(wrapper, enabled=True, fail_mode="unresolved", max_calls_per_unit=5)
    return verifier, wrapper, sent


async def test_verifier_observer_crosses_coordination_once_per_attempt():
    verifier, wrapper, sent = verifier_and_chat()
    budget = ReviewRunBudget("run", max_tokens=100000, max_calls=10)
    ids = []
    async def observe(record, metadata):
        ids.append(record.attempt_id)
    token = run_budget_attempt_observer.set(observe)
    try:
        with activate_run_budget(budget):
            result = await verifier.verify_issues([_issue("i")], [_unit()], _state(_unit()), IssueMetrics())
    finally:
        run_budget_attempt_observer.reset(token)
    assert result.issues[0].status == "confirmed"
    assert result.metrics.verifier_call_count == budget.calls_consumed == len(sent) == 1
    assert result.metrics.verifier_token_count == 15
    assert ids == list(budget.attempts)
    assert next(iter(wrapper.runtime.data["calls"].values()))["attempt_ids"] == ids


@pytest.mark.parametrize("interrupt", [False, True])
async def test_verification_replay_ignores_remaining_call_counter(persistence, interrupt):
    repository, _, _ = persistence
    repository.create_task(ReviewTask(id="run", pr_url="https://github.com/a/b/pull/1"))
    verifier, wrapper, sent = verifier_and_chat()
    wrapper.runtime = CoordinationRuntime("run", "a" * 64, ExecutionBudget(), repository)
    await wrapper.runtime.load()
    issue, unit = _issue("i"), _unit()
    budget = ReviewRunBudget("run", max_tokens=100000, max_calls=10)
    with activate_run_budget(budget):
        first = verifier._build_request(issue, unit, _state(unit), 0)
        if interrupt:
            def stop(_state):
                if any(item["status"] == "completed" for item in wrapper.runtime.data["calls"].values()):
                    raise asyncio.CancelledError()
            wrapper.runtime.progress = stop
            with pytest.raises(asyncio.CancelledError):
                await wrapper.verify_issue(first, "fixture")
        else:
            await wrapper.verify_issue(first, "fixture")
        restarted = CoordinationRuntime("run", "a" * 64, ExecutionBudget(), repository)
        await restarted.load()
        wrapper.runtime = restarted
        second = verifier._build_request(issue, unit, _state(unit), 0)
        assert first.budget.remaining_calls != second.budget.remaining_calls
        assert OpenAICompatibleProvider._build_issue_verification_prompt(first) == OpenAICompatibleProvider._build_issue_verification_prompt(second)
        await wrapper.verify_issue(second, "fixture")
        assert len(sent) == budget.calls_consumed == 1
        assert len(restarted.data["calls"]) == 1
        assert restarted.data["cache_hits"] == 1
        # An evidence change must create another identity, even for the same issue.
        different = second.model_copy(update={"unit_diff": second.unit_diff + "\nchanged evidence"})
        await wrapper.verify_issue(different, "fixture")
        assert len(sent) == budget.calls_consumed == 2
