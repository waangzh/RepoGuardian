import asyncio

import pytest

from app.models.review import ExecutionBudget, ModelUsage
from app.services.model_request_budgeter import UnitRequestLedger
from app.services.review_admission import budget_fit
from app.services.review_runtime import DeferredTaskQueue, ReviewTask, ReviewTaskStatus


def _usage(total: int) -> ModelUsage:
    return ModelUsage(provider="fixture", model="fixture", operation="review_step",
                      actual_total_tokens=total, actual_input_tokens=total,
                      actual_output_tokens=0, usage_available=True,
                      accounting_source="actual", latency_ms=0)


def test_budget_fit_does_not_turn_balance_shortage_into_split_permission():
    budget = ExecutionBudget(model_calls=1, max_model_calls=3, token_usage=900,
                             max_token_usage=1000, diagnosis_attempts=1,
                             max_diagnosis_attempts=3)
    fit = budget_fit(budget, model_calls=1, diagnosis_attempts=1, token_usage=200)
    assert not fit.admitted
    assert fit.reason == "budget_tokens_insufficient"
    assert fit.can_defer and fit.kind.value == "budget"


def test_unit_ledger_settles_each_attempt_against_its_own_reservation():
    ledger = UnitRequestLedger(ExecutionBudget(max_model_calls=3, max_token_usage=1000))
    import asyncio
    asyncio.run(ledger.reserve({"reserved_tokens": 400}))
    asyncio.run(ledger.reserve({"reserved_tokens": 100}))
    ledger.settle(_usage(50))
    ledger.settle(_usage(350))
    assert ledger.budget.token_usage == 400
    assert [event["released_tokens"] for event in ledger.settlement_events] == [350, 0]


def test_unsent_reservation_releases_tokens_and_call_slot():
    ledger = UnitRequestLedger(ExecutionBudget(max_model_calls=2, max_token_usage=500))
    import asyncio
    asyncio.run(ledger.reserve({"reserved_tokens": 200}))
    event = ledger.release_unsent()
    assert event["released_tokens"] == 200
    assert ledger.budget.model_calls == 0 and ledger.budget.token_usage == 0


@pytest.mark.asyncio
async def test_deferred_queue_drains_only_after_revision_changes():
    queue = DeferredTaskQueue()
    task = queue.defer(ReviewTask("t1", "review_step", priority=(1,)),
                       reason="budget_tokens_insufficient", budget_revision=1)
    calls = []

    async def execute(item):
        calls.append(item.task_id)

    assert await queue.drain(budget_revision=1, admit=lambda _: {"admitted": True}, execute=execute) == []
    done = await queue.drain(budget_revision=2, admit=lambda _: {"admitted": True}, execute=execute)
    assert [item.task_id for item in done] == ["t1"]
    assert calls == ["t1"] and task.status == ReviewTaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_deferred_queue_keeps_task_when_budget_still_unavailable():
    queue = DeferredTaskQueue()
    queue.defer(ReviewTask("t1", "verify_issue"), reason="budget_calls_insufficient", budget_revision=2)
    done = await queue.drain(budget_revision=3, admit=lambda _: {"admitted": False, "reason": "budget_calls_insufficient"},
                             execute=lambda _: pytest.fail("must not execute"))
    assert done == [] and queue.pending()[0].reason == "budget_calls_insufficient"


@pytest.mark.asyncio
async def test_deferred_queue_accepts_structured_budget_fit_without_dict_access_error():
    queue = DeferredTaskQueue()
    queue.defer(ReviewTask("t1", "review_step"), reason="budget", budget_revision=1)
    fit = budget_fit(ExecutionBudget(max_token_usage=100), token_usage=1)
    done = await queue.drain(budget_revision=2, admit=lambda _: fit,
                             execute=lambda _: asyncio.sleep(0))
    assert [item.task_id for item in done] == ["t1"]
