import asyncio

import pytest

from app.services.review_run_budget import (
    AttemptState,
    ReviewRunBudget,
    RunBudgetError,
)


class Store:
    def __init__(self):
        self.rows = {}

    def load(self, run_id):
        return self.rows.get(run_id)

    def save(self, run_id, payload, revision):
        self.rows[run_id] = payload


@pytest.mark.asyncio
async def test_concurrent_reservations_have_one_winner():
    budget = ReviewRunBudget("run", max_tokens=100, max_calls=2)

    async def reserve(index):
        try:
            return await budget.reserve(request_id=f"r{index}", operation="review_step",
                                        estimated_tokens=60)
        except RunBudgetError:
            return None

    attempts = await asyncio.gather(*(reserve(i) for i in range(2)))
    assert sum(item is not None for item in attempts) == 1
    assert budget.in_flight_reserved == 60 and budget.calls_consumed == 1


@pytest.mark.asyncio
async def test_actual_settlement_releases_estimate_and_records_overrun():
    budget = ReviewRunBudget("run", max_tokens=100, max_calls=2)
    attempt = await budget.reserve(request_id="r", operation="verify_issue", estimated_tokens=60)
    await budget.mark_dispatching(attempt.attempt_id)
    settled = await budget.settle(attempt.attempt_id, actual_tokens=40)
    assert settled.state == AttemptState.SETTLED
    assert settled.released_tokens == 20
    assert budget.in_flight_reserved == 0 and budget.settled_actual == 40
    assert budget.available()["tokens"] == 60


@pytest.mark.asyncio
async def test_unknown_usage_is_reserved_until_reconciled():
    budget = ReviewRunBudget("run", max_tokens=100, max_calls=2)
    attempt = await budget.reserve(request_id="r", operation="review_step", estimated_tokens=70)
    await budget.mark_dispatching(attempt.attempt_id)
    unknown = await budget.settle(attempt.attempt_id, actual_tokens=None)
    assert unknown.state == AttemptState.UNKNOWN
    assert budget.unknown_reserved == 70 and budget.available()["tokens"] == 30
    resolved = await budget.reconcile_unknown(attempt.attempt_id, actual_tokens=50)
    assert resolved.state == AttemptState.SETTLED
    assert budget.unknown_reserved == 0 and budget.settled_actual == 50


@pytest.mark.asyncio
async def test_unsent_release_does_not_consume_call_or_tokens():
    budget = ReviewRunBudget("run", max_tokens=100, max_calls=1)
    attempt = await budget.reserve(request_id="r", operation="review_step", estimated_tokens=80)
    released = await budget.release_unsent(attempt.attempt_id)
    assert released.state == AttemptState.RELEASED
    assert budget.calls_consumed == 0 and budget.available() == {"tokens": 100, "calls": 1}


@pytest.mark.asyncio
async def test_obligation_conversion_is_single_consumer():
    budget = ReviewRunBudget("run", max_tokens=100, max_calls=2)
    obligation = await budget.create_obligation(owner_task_id="task", token_budget=60,
                                                reason="independent verification")
    converted = await budget.convert_obligation(obligation.obligation_id, request_id="r",
                                                operation="verify_issue", estimated_tokens=60)
    assert converted.reserved_tokens == 60
    with pytest.raises(RunBudgetError, match="obligation_not_convertible"):
        await budget.convert_obligation(obligation.obligation_id, request_id="r2",
                                        operation="verify_issue", estimated_tokens=1)


def test_restore_marks_send_boundary_as_unknown_without_freeing_reservation():
    payload = ReviewRunBudget("run", max_tokens=100, max_calls=1).snapshot()
    payload["attempts"] = {
        "a": {
            "attempt_id": "a", "request_id": "r", "run_id": "run", "task_id": None,
            "operation": "review_step", "request_hash": "h", "estimated_tokens": 90,
            "reserved_tokens": 90, "output_tokens": 10, "state": "dispatching",
            "actual_tokens": None, "released_tokens": 0, "overrun_tokens": 0,
            "usage_available": False, "settled": False, "sent": True,
            "created_at": "now", "settled_at": None,
        }
    }
    payload["in_flight_reserved"] = 90
    payload["calls_consumed"] = 1
    restored = ReviewRunBudget.restore(payload)
    assert restored.attempts["a"].state == AttemptState.UNKNOWN
    assert restored.unknown_reserved == 90 and restored.available()["tokens"] == 10


@pytest.mark.asyncio
async def test_load_or_create_reuses_persisted_consumption():
    store = Store()
    budget = await ReviewRunBudget.load_or_create("run", max_tokens=100, max_calls=2, store=store)
    await budget.reserve(request_id="r", operation="review_step", estimated_tokens=40)
    restored = await ReviewRunBudget.load_or_create("run", max_tokens=100, max_calls=2, store=store)
    assert restored.in_flight_reserved == 40
    assert restored.revision == budget.revision


@pytest.mark.asyncio
async def test_cost_limit_is_independent_from_token_limit():
    budget = ReviewRunBudget("run", max_tokens=1000, max_calls=2, max_cost_microusd=100)
    await budget.reserve(request_id="r", operation="review_step", estimated_tokens=10,
                         estimated_cost_microusd=80)
    with pytest.raises(RunBudgetError, match="run_budget_cost_rejected"):
        await budget.reserve(request_id="r2", operation="review_step", estimated_tokens=10,
                             estimated_cost_microusd=30)
