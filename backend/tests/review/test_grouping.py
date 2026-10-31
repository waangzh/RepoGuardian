import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sqlalchemy import delete, select, update

from app.models.orm import ReviewRequestOrm, ReviewTaskOrm, ReviewWorkItemOrm, WorkerJobOrm
from app.models.review.diff import ChangedFile
from app.review.context import AgentContext
from app.review.domain import utcnow
from app.review.grouping import FileGroup, GroupingOutput, freeze_semantic_groups, grouped_plan
from app.review.planner import plan_review
from app.review.store import BudgetExceeded, LeaseLost, ReviewStopped
from tests.review.vnext_helpers import setup_run


def changes(*paths):
    return [ChangedFile(file_path=path, change_type="modified", additions=1, deletions=0)
            for path in paths]


def test_grouping_output_assigns_each_file_once_and_preserves_label(tmp_path):
    store, run, lease, _ = setup_run(tmp_path)
    run.changed_files = changes("auth/controller.py", "auth/service.py", "auth/test.py", "orders.py")
    run.plan = plan_review(run.changed_files, run.snapshot, run.configuration.options)
    run.plan.grouping_status = "pending"
    output = GroupingOutput(groups=[
        FileGroup(label="Authentication", files=[2, 0, 0, 99]),
    ])

    plan = grouped_plan(run, output)

    assert sorted(path for unit in plan.units for path in unit.paths) == sorted(
        change.file_path for change in run.changed_files)
    auth = next(unit for unit in plan.units if "auth/controller.py" in unit.paths)
    assert auth.label == "Authentication"
    assert plan.grouping_status == "model"


def test_grouping_failure_falls_back_to_single_file_units(tmp_path):
    _, run, _, _ = setup_run(tmp_path)
    run.changed_files = changes("a.py", "b.py", "c.py", "d.py")
    run.plan = plan_review(run.changed_files, run.snapshot, run.configuration.options)
    run.plan.grouping_status = "pending"

    plan = grouped_plan(run, reason="grouping_unavailable")

    assert [unit.paths for unit in plan.units] == [[path] for path in ("a.py", "b.py", "c.py", "d.py")]
    assert plan.grouping_status == "fallback"
    assert plan.grouping_reason == "grouping_unavailable"


def test_repeated_immutable_tool_result_is_marked_as_stalled():
    context = AgentContext(provider=None, work=None, budget=None)
    arguments = {"path": "a.py", "revision": "head", "start": 1, "end": 2}
    result = {"ok": True, "source": {"commit_sha": "head-sha", "side": "head", "path": "a.py",
                                   "start_line": 1, "end_line": 2},
              "content": "VALUE = 1\n", "source_ref": "S1"}

    assert context.note_tool_result("read_file", arguments, result) == 1
    assert context.note_tool_result("read_file", arguments, result) == 2
    assert context.note_tool_result("read_file", arguments, result) == 3


def test_duplicate_source_bodies_are_compacted_but_handles_are_kept():
    source = {
        "commit_sha": "a" * 40, "side": "head", "path": "a.py",
        "review_id": "review", "content_hash": "b" * 64,
        "start_line": 1, "end_line": 2, "id": "c" * 64,
    }
    messages = [
        HumanMessage(content=json.dumps({
            "materials": [{"sources": [{"source": source, "content": "VALUE = 1\n"}]}],
        })),
        ToolMessage(content=json.dumps({
            "source": source, "source_ref": "S1", "content": "VALUE = 1\n",
        }), tool_call_id="call-1"),
    ]
    compacted = AgentContext.compact_messages(messages)
    payload = json.loads(compacted[1].content)
    assert payload["source_ref"] == "S1"
    assert payload["content_omitted"] is True
    assert "VALUE = 1" not in payload["content"]


def pending_run(tmp_path):
    store, run, lease, sessions = setup_run(tmp_path)
    run.changed_files = changes("a.py", "b.py", "c.py", "d.py")
    run.plan = plan_review(run.changed_files, run.snapshot, run.configuration.options)
    run.plan.grouping_status = "pending"
    with sessions.begin() as session:
        session.execute(delete(ReviewWorkItemOrm).where(ReviewWorkItemOrm.run_id == run.id))
        row = session.get(ReviewTaskOrm, run.id)
        row.task_snapshot = run.model_dump(mode="json")
    return store, run, lease, sessions


class GroupModel:
    def __init__(self, *, invalid=False, unknown=False):
        self.calls = []
        self.invalid, self.unknown = invalid, unknown

    def bind_tools(self, tools, **kwargs):
        assert tools == [GroupingOutput]
        return self

    async def ainvoke(self, messages):
        self.calls.append(messages)
        return AIMessage(content="", usage_metadata=(None if self.unknown else {
            "input_tokens": 80, "output_tokens": 20, "total_tokens": 100}), tool_calls=[{
                "id": "groups", "name": "GroupingOutput",
                "args": {"groups": "invalid"} if self.invalid else {
                    "groups": [{"label": "Feature", "files": [0, 3]},
                               {"label": "Support", "files": [1, 2]}]},
            }])


async def test_grouping_is_frozen_before_dispatch_and_not_replayed(tmp_path):
    store, run, lease, sessions = pending_run(tmp_path)
    model = GroupModel()
    assert store.work_items(run.id) == []
    frozen = await freeze_semantic_groups(run, store, lease, model)
    assert frozen.plan.grouping_status == "model"
    assert len(store.work_items(run.id)) == 2
    assert any(unit.paths == ["a.py", "d.py"] for unit in frozen.plan.units)
    again = await freeze_semantic_groups(store.get(run.id), store, lease, model)
    assert again.plan == frozen.plan
    assert len(model.calls) == 1
    assert len(store.work_items(run.id)) == 2
    packet = json.loads(model.calls[0][1].content)
    assert set(packet["files"][0]) == {"index", "path", "status", "additions", "deletions"}
    assert store.usage(run.id)["input_tokens"] == 80
    with sessions() as session:
        ticket = session.scalar(select(ReviewRequestOrm))
        assert ticket.kind == "grouping" and ticket.state == "settled"


async def test_unknown_grouping_request_is_not_recharged_after_recovery(tmp_path):
    store, run, lease, _ = pending_run(tmp_path)
    store.reserve_grouping(run.id, 1234, lease)
    reopened = type(store)(store.sessions)
    model = GroupModel()
    frozen = await freeze_semantic_groups(reopened.get(run.id), reopened, lease, model)
    assert frozen.plan.grouping_status == "fallback"
    assert frozen.plan.grouping_reason == "previous_grouping_request_not_replayed"
    assert model.calls == []
    assert reopened.usage(run.id)["unknown_calls"] == 1
    assert reopened.usage(run.id)["committed_input_tokens"] == 1234
    assert len(reopened.work_items(run.id)) == 4


@pytest.mark.parametrize("unknown", [False, True])
async def test_invalid_grouping_keeps_usage_and_falls_back(tmp_path, unknown):
    store, run, lease, _ = pending_run(tmp_path)
    frozen = await freeze_semantic_groups(run, store, lease, GroupModel(invalid=True, unknown=unknown))
    assert frozen.plan.grouping_status == "fallback"
    assert len(store.work_items(run.id)) == 4
    assert store.usage(run.id)["unknown_calls"] == int(unknown)
    assert store.usage(run.id)["model_calls"] == 1
    assert store.usage(run.id)["committed_input_tokens"] > 0


def test_concurrent_grouping_reservation_is_single_and_freeze_is_idempotent(tmp_path):
    store, run, lease, _ = pending_run(tmp_path)
    with ThreadPoolExecutor(2) as pool:
        tickets = list(pool.map(lambda _: store.reserve_grouping(run.id, 1000, lease), range(2)))
    assert sum(ticket is not None for ticket in tickets) == 1
    plan = grouped_plan(run)
    with ThreadPoolExecutor(2) as pool:
        plans = list(pool.map(lambda _: store.freeze_grouping(run.id, plan, lease), range(2)))
    assert plans[0].plan == plans[1].plan
    assert len(store.work_items(run.id)) == 4


@pytest.mark.parametrize("fault", ["missing", "path", "reused", "duplicate"])
def test_grouping_freeze_refuses_invalid_scope_atomically(tmp_path, fault):
    store, run, lease, _ = pending_run(tmp_path)
    plan = grouped_plan(run)
    if fault == "missing":
        plan.units.pop()
    elif fault == "path":
        plan.units[0].paths = ["untrusted.py"]
    elif fault == "reused":
        plan.units[0].reused_from = "untrusted:work"
    else:
        plan.units.append(plan.units[0])
    with pytest.raises(ValueError, match="grouping_"):
        store.freeze_grouping(run.id, plan, lease)
    assert store.get(run.id).plan.grouping_status == "pending"
    assert store.work_items(run.id) == []


@pytest.mark.parametrize("stop", ["cancel", "lease", "deadline"])
def test_grouping_freeze_rechecks_lifecycle_fence(tmp_path, stop):
    store, run, lease, sessions = pending_run(tmp_path)
    plan = grouped_plan(run)
    if stop == "cancel":
        store.cancel(run.id)
        expected = ReviewStopped
    elif stop == "lease":
        with sessions.begin() as session:
            session.execute(update(WorkerJobOrm).values(lease_until=utcnow() - timedelta(seconds=1)))
        expected = LeaseLost
    else:
        run.hard_deadline = utcnow() - timedelta(seconds=1)
        with sessions.begin() as session:
            session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
        expected = BudgetExceeded
    with pytest.raises(expected):
        store.freeze_grouping(run.id, plan, lease)
    assert store.work_items(run.id) == []


async def test_cancelled_grouping_stays_unknown_and_does_not_dispatch(tmp_path):
    store, run, lease, _ = pending_run(tmp_path)
    entered = asyncio.Event()

    class WaitingModel(GroupModel):
        async def ainvoke(self, messages):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(freeze_semantic_groups(run, store, lease, WaitingModel()))
    await entered.wait()
    store.cancel(run.id)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.usage(run.id)["unknown_calls"] == 1
    assert store.work_items(run.id) == []


def test_no_progress_tracks_rounds_and_resets_when_investigation_advances():
    context = AgentContext(provider=None, work=None, budget=None)
    request, response = {"path": "a.py"}, {"content": "same"}
    context.note_tool_result("read_file", request, response)
    assert context.begin_round() == 0
    for round_number in (1, 2, 3):
        context.note_tool_result("read_file", request, response)
        assert context.begin_round() == round_number
    context.note_tool_result("read_file", {"path": "b.py"}, response)
    assert context.begin_round() == 0
    context.note_tool_result("read_file", request, response)
    assert context.begin_round() == 1


def test_pagination_and_base_head_comparison_are_progress():
    context = AgentContext(provider=None, work=None, budget=None)
    for index in range(5):
        cursor = str(index)
        context.cursors[cursor] = {"scope": "search", "position": index, "column": 0}
        context.note_tool_result("search_code", {"cursor": cursor}, {"matches": [], "next_cursor": str(index + 1)})
        assert context.begin_round() == 0
    for side in ("base", "head"):
        context.note_tool_result("read_file", {"path": "a.py", "revision": side}, {"content": "same"})
        assert context.begin_round() == 0


def test_opaque_cursor_changes_do_not_count_as_progress():
    context = AgentContext(provider=None, work=None, budget=None)
    for index in range(4):
        cursor = str(index)
        context.cursors[cursor] = {"scope": "search", "position": 0, "column": 0}
        context.note_tool_result("search_code", {"cursor": cursor},
                                 {"matches": [], "cursor": cursor, "next_cursor": str(index + 1)})
        assert context.begin_round() == index


def test_overlapping_source_requests_require_new_lines_to_count_as_progress():
    context = AgentContext(provider=None, work=None, budget=None)

    def read(start, end, side="head", tool="read_file"):
        source = {"commit_sha": side + "-sha", "side": side, "path": "a.py",
                  "start_line": start, "end_line": end}
        context.note_tool_result(tool, {"start": start, "end": end, "revision": side},
                                 {"ok": True, "source": source, "content": "fixed bytes"})
        return context.begin_round()

    assert read(1, 100) == 0
    assert read(101, 200) == 0
    assert read(20, 180) == 1
    assert read(30, 170, tool="read_symbol") == 2
    assert read(40, 160) == 3
    assert read(150, 220) == 0
    assert read(40, 160, side="base") == 0


def test_pending_grouping_cannot_be_reported_as_completed(tmp_path):
    from app.review.report import build_report

    store, run, _, _ = pending_run(tmp_path)
    report = build_report(run, [], [], store.usage(run.id))
    assert report.status == "partial"
    assert "grouping_incomplete" in report.reasons
    assert "scope_empty" not in report.reasons
