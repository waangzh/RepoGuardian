import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import update

from app.models.orm import ReviewTaskOrm
from app.review.agent import AgentRunner
from app.review.domain import utcnow
from app.review.index import OnDemandIndex
from app.review.orchestrator import ReviewOrchestrator
from app.review.store import ReviewStopped
from tests.review.test_orchestrator import ReviewModel
from tests.review.vnext_helpers import setup_source_run


def set_deadlines(sessions, run, *, cutoff=None, deadline=None):
    if cutoff:
        run.review_cutoff = cutoff
    if deadline:
        run.hard_deadline = deadline
    with sessions.begin() as session:
        session.execute(update(ReviewTaskOrm).where(ReviewTaskOrm.id == run.id).values(
            task_snapshot=run.model_dump(mode="json")))


async def test_soft_cutoff_skips_discovery_without_claiming_complete(tmp_path):
    store, run, lease, provider, sessions = setup_source_run(tmp_path)
    set_deadlines(sessions, run, cutoff=utcnow() - timedelta(seconds=1))
    report = await ReviewOrchestrator(store, AgentRunner(ReviewModel())).run(run.id, provider, lease)
    assert report.status == "partial"
    assert report.units[0].status == "skipped"
    assert report.units[0].error_code == "review_cutoff"
    assert report.usage["model_calls"] == 1  # Only the attempted default overview.


async def test_hard_cutoff_never_sends_a_model_request(tmp_path):
    store, run, lease, provider, sessions = setup_source_run(tmp_path)
    set_deadlines(sessions, run, deadline=utcnow() - timedelta(seconds=1))
    report = await ReviewOrchestrator(store, AgentRunner(ReviewModel())).run(run.id, provider, lease)
    assert report.status == "partial"
    assert report.overview.status == "skipped_deadline"
    assert report.usage["model_calls"] == 0


async def test_completed_reviewer_is_reused_after_process_restart(tmp_path):
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    from app.review.budget import BudgetGate
    work = store.claim(run.id, store.work_items(run.id)[0].id, lease)
    await AgentRunner(ReviewModel()).run(work, provider, BudgetGate(store, run, lease))
    assert len(store.candidates(run.id)) == 1
    assert store.usage(run.id)["model_calls"] == 2
    reopened = type(store)(store.sessions)
    provider.store = reopened
    report = await ReviewOrchestrator(reopened, AgentRunner(ReviewModel())).run(run.id, provider, lease)
    assert len(report.candidates) == 1
    assert report.usage["model_calls"] == 4
    assert report.units[0].attempt == 1


async def test_cancel_stops_inflight_work_and_does_not_finish_run(tmp_path):
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    entered, drained = asyncio.Event(), asyncio.Event()
    class WaitingRunner:
        model = ReviewModel()
        async def run(self, *args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()
    task = asyncio.create_task(ReviewOrchestrator(store, WaitingRunner()).run(run.id, provider, lease))
    await entered.wait()
    store.cancel(run.id)
    with pytest.raises(ReviewStopped):
        await task
    assert drained.is_set()
    assert store.get(run.id).status == "cancelled"
    assert store.get(run.id).report is None


async def test_index_is_query_driven_and_reuses_fixed_source(tmp_path):
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    index = OnDemandIndex(provider.evidence)
    assert index.analyses == {}
    first = await index.symbol("a.py", "ratio")
    (provider.evidence.repo_path / "a.py").write_text("raise RuntimeError('dirty')\n", encoding="utf-8")
    second = await index.symbol("a.py", "ratio")
    assert first == second
    assert len(index.analyses) == 1
    result = await index.relations("a.py", "ratio", "both")
    assert result["scope_complete"] is False
    assert result["parsed_files"] == 1
