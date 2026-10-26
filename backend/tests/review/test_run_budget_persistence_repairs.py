import asyncio
from copy import deepcopy

import pytest

from app.models.review import ReviewTask
from app.services.review.coordination_runtime import CoordinationLeaseLost
from app.services.review.review_run_budget import (
    AttemptState, ReviewRepositoryRunBudgetStore, ReviewRunBudget, RunBudgetError,
)
from tests.support.persistence import persistence as persistence


@pytest.mark.parametrize("running", [False, True])
async def test_repository_first_reservation_and_recovery(persistence, running):
    repository, _, _ = persistence
    repository.create_task(ReviewTask(id="run", pr_url="https://github.com/a/b/pull/1"))
    store = ReviewRepositoryRunBudgetStore(repository)
    budget = await ReviewRunBudget.load_or_create("run", max_tokens=100, max_calls=3, store=store)
    assert store.load("run")["revision"] == 0
    if running:
        attempt = await budget.reserve(request_id="r", operation="review_step", estimated_tokens=30)
        await budget.mark_dispatching(attempt.attempt_id)
    restored = await ReviewRunBudget.load_or_create(
        "run", max_tokens=999, max_calls=999, store=store, recover_running=True)
    assert restored.snapshot() == store.load("run")
    assert restored.max_tokens == 100
    await restored.reserve(request_id="next", operation="review_step", estimated_tokens=20)
    assert restored.snapshot() == store.load("run")
    assert restored.unknown_reserved == (30 if running else 0)


class FailingStore:
    def __init__(self):
        self.payload = None
        self.error = None

    def load(self, run_id):
        return deepcopy(self.payload)

    def save(self, run_id, payload, revision):
        if self.error:
            error, self.error = self.error, None
            raise error
        assert revision == (self.payload["revision"] + 1 if self.payload else 0)
        self.payload = deepcopy(payload)


@pytest.mark.parametrize("operation", ["reserve", "settle", "mark_dispatching", "release_unsent",
                                      "create_obligation", "release_obligation", "convert_obligation"])
async def test_failed_write_rolls_back_and_can_retry(operation):
    store = FailingStore()
    budget = await ReviewRunBudget.load_or_create("run", max_tokens=100, max_calls=3, store=store)
    attempt = await budget.reserve(request_id="r", operation="review_step", estimated_tokens=20)
    obligation = await budget.create_obligation(owner_task_id="t", token_budget=10, reason="retry")
    actions = {
        "reserve": lambda: budget.reserve(request_id="next", operation="review_step", estimated_tokens=10),
        "settle": lambda: budget.settle(attempt.attempt_id, actual_tokens=5),
        "mark_dispatching": lambda: budget.mark_dispatching(attempt.attempt_id),
        "release_unsent": lambda: budget.release_unsent(attempt.attempt_id),
        "create_obligation": lambda: budget.create_obligation(owner_task_id="t2", token_budget=5, reason="retry"),
        "release_obligation": lambda: budget.release_obligation(obligation.obligation_id),
        "convert_obligation": lambda: budget.convert_obligation(obligation.obligation_id, request_id="next",
            operation="verify_issue", estimated_tokens=10),
    }
    before = budget.snapshot()
    store.error = OSError("write failed")
    with pytest.raises(OSError):
        await actions[operation]()
    assert budget.snapshot() == store.payload == before
    await actions[operation]()
    assert budget.snapshot() == store.payload
    assert budget.revision == before["revision"] + 1


@pytest.mark.parametrize("error", [CoordinationLeaseLost("lease lost"), asyncio.CancelledError()])
async def test_cancelled_persistence_invalidates_instance(error):
    store = FailingStore()
    budget = await ReviewRunBudget.load_or_create("run", max_tokens=100, max_calls=2, store=store)
    attempt = await budget.reserve(request_id="r", operation="review_step", estimated_tokens=20)
    await budget.mark_dispatching(attempt.attempt_id)
    store.error = error
    with pytest.raises(asyncio.CancelledError):
        await budget.settle(attempt.attempt_id, actual_tokens=5)
    assert budget.attempts[attempt.attempt_id].state == AttemptState.DISPATCHING
    with pytest.raises(RunBudgetError, match="requires_reload"):
        await budget.settle(attempt.attempt_id, actual_tokens=5)


async def test_direct_restore_persists_recovery_before_next_write():
    store = FailingStore()
    budget = await ReviewRunBudget.load_or_create("run", max_tokens=100, max_calls=2, store=store)
    await budget.reserve(request_id="r", operation="review_step", estimated_tokens=20)
    restored = ReviewRunBudget.restore(store.load("run"), store=store)
    await restored.reserve(request_id="next", operation="review_step", estimated_tokens=10)
    assert restored.snapshot() == store.payload
    assert restored.unknown_reserved == 20
