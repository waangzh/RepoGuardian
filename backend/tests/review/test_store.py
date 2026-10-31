from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.models.orm import ReviewTaskOrm, WorkerJobOrm
from app.review.domain import utcnow
from app.review.store import BudgetExceeded, LeaseLost, ReviewStopped
from tests.review.vnext_helpers import setup_run, submit_candidate


def test_candidate_and_verification_are_atomic_and_tool_idempotent(tmp_path):
    store, run, lease, _ = setup_run(tmp_path)
    work = store.claim(run.id, store.work_items(run.id)[0].id, lease)
    candidate = submit_candidate(store, run, lease, work)
    with ThreadPoolExecutor(2) as pool:
        ids = list(pool.map(lambda _: store.submit(run.id, work, candidate, lease), range(2)))
    assert ids == [candidate.id, candidate.id]
    assert len(store.candidates(run.id)) == 1
    assert len([item for item in store.work_items(run.id) if item.kind == "verification"]) == 1


def test_parallel_requests_leave_overview_and_pending_verification(tmp_path):
    store, run, lease, _ = setup_run(tmp_path, calls=4)
    work = store.claim(run.id, store.work_items(run.id)[0].id, lease)
    submit_candidate(store, run, lease, work)
    def reserve(_):
        try:
            return store.reserve(run.id, work, 1000, lease)
        except BudgetExceeded:
            return None
    with ThreadPoolExecutor(3) as pool:
        result = list(pool.map(reserve, range(3)))
    assert sum(value is not None for value in result) == 2
    check = next(item for item in store.work_items(run.id) if item.kind == "verification")
    check = store.claim(run.id, check.id, lease)
    store.reserve(run.id, check, 1000, lease)
    store.reserve(run.id, None, 1000, lease, overview=True)
    assert store.usage(run.id)["model_calls"] == 4


def test_unknown_usage_stays_reserved_after_reopen_and_settle_is_idempotent(tmp_path):
    store, run, lease, sessions = setup_run(tmp_path)
    work = store.claim(run.id, store.work_items(run.id)[0].id, lease)
    ticket = store.reserve(run.id, work, 4000, lease)
    store.settle(run.id, ticket, None, lease)
    store.settle(run.id, ticket, {"input_tokens": 0, "output_tokens": 0}, lease)
    reopened = type(store)(sessions)
    assert reopened.usage(run.id)["input_tokens"] is None
    assert reopened.usage(run.id)["committed_input_tokens"] == 4000


@pytest.mark.parametrize("stop", ["cancel", "lease", "attempt"])
def test_late_candidate_cannot_write(tmp_path, stop):
    store, run, lease, sessions = setup_run(tmp_path)
    work = store.claim(run.id, store.work_items(run.id)[0].id, lease)
    candidate = submit_candidate(store, run, lease, work)
    if stop == "cancel":
        store.cancel(run.id)
        expected = ReviewStopped
    elif stop == "lease":
        with sessions.begin() as session:
            session.execute(update(WorkerJobOrm).values(lease_until=utcnow() - timedelta(seconds=1)))
        expected = LeaseLost
    else:
        store.begin(run.id, lease)
        store.claim(run.id, work.id, lease)
        expected = LeaseLost
    with pytest.raises(expected):
        store.submit(run.id, work, candidate, lease)
    assert len(store.candidates(run.id)) == 1


def test_create_intent_retries_return_one_run_and_conflict_on_payload(tmp_path):
    store, run, _, sessions = setup_run(tmp_path)
    assert store.create(run, "local", "key", "hash") == run.id
    with pytest.raises(ValueError, match="idempotency_key_conflict"):
        store.create(run, "local", "key", "other")
    with sessions() as session:
        assert len(list(session.scalars(select(ReviewTaskOrm)))) == 1


def test_hard_deadline_blocks_overview_even_with_reserved_budget(tmp_path):
    store, run, lease, sessions = setup_run(tmp_path)
    with sessions.begin() as session:
        row = session.get(ReviewTaskOrm, run.id)
        payload = dict(row.task_snapshot)
        payload["hard_deadline"] = (utcnow() - timedelta(seconds=1)).isoformat()
        row.task_snapshot = payload
    with pytest.raises(BudgetExceeded, match="hard_deadline"):
        store.reserve(run.id, None, 1000, lease, overview=True)


def test_retried_verification_gets_a_new_minimum_before_discovery(tmp_path):
    store, run, lease, _ = setup_run(tmp_path, calls=3)
    reviewer = store.claim(run.id, store.work_items(run.id)[0].id, lease)
    submit_candidate(store, run, lease, reviewer)
    check = next(work for work in store.work_items(run.id) if work.kind == "verification")
    check = store.claim(run.id, check.id, lease)
    ticket = store.reserve(run.id, check, 1000, lease)
    store.settle(run.id, ticket, None, lease)
    store.begin(run.id, lease)
    reviewer = store.claim(run.id, reviewer.id, lease)
    with pytest.raises(BudgetExceeded, match="model_call_limit"):
        store.reserve(run.id, reviewer, 1000, lease)
    check = store.claim(run.id, check.id, lease)
    store.reserve(run.id, check, 1000, lease)


def test_late_completion_cannot_turn_hard_timeout_into_completed(tmp_path):
    store, run, lease, sessions = setup_run(tmp_path)
    work = store.claim(run.id, store.work_items(run.id)[0].id, lease)
    with sessions.begin() as session:
        row = session.get(ReviewTaskOrm, run.id)
        payload = dict(row.task_snapshot)
        payload["hard_deadline"] = (utcnow() - timedelta(seconds=1)).isoformat()
        row.task_snapshot = payload
    with pytest.raises(BudgetExceeded, match="hard_deadline"):
        store.deliver(run.id, work, ["h1"], {}, lease)
    store.finish_work(run.id, work, "completed", {}, None, lease)
    saved = store.work_items(run.id)[0]
    assert saved.status == "interrupted"
    assert saved.error_code == "hard_deadline"


def test_request_budget_snapshot_keeps_verification_overview_and_unknown_usage(tmp_path):
    store, run, lease, _ = setup_run(tmp_path, calls=4, input_tokens=30000)
    work = store.claim(run.id, store.work_items(run.id)[0].id, lease)
    submit_candidate(store, run, lease, work)
    first, available = store.reserve(run.id, work, 1000, lease, with_remaining=True)
    assert available["input_tokens"] == 4000
    assert available["model_calls"] == 2
    assert available["output_tokens"] == run.configuration.options.limits.total_output_tokens - 8192
    store.settle(run.id, first, None, lease)
    second, available = store.reserve(run.id, work, 1000, lease, with_remaining=True)
    assert second != first
    assert available["input_tokens"] == 3000
    assert available["model_calls"] == 1
    with pytest.raises(BudgetExceeded, match="model_call_limit"):
        store.reserve(run.id, work, 1000, lease)
    check = next(item for item in store.work_items(run.id) if item.kind == "verification")
    check = store.claim(run.id, check.id, lease)
    store.reserve(run.id, check, 1000, lease)
    store.reserve(run.id, None, 1000, lease, overview=True)
    usage = store.usage(run.id)
    assert usage["model_calls"] == 4
    assert usage["committed_input_tokens"] == 4000
    assert usage["input_tokens"] is None
