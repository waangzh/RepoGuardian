import asyncio
import hashlib
import json
from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models.orm import ArtifactOrm, SideEffectOrm, WorkerJobOrm, utcnow
from app.models.review import ReviewTask
from app.models.review_runtime import ReviewGoal, ReviewWorkItem
from app.services.review.coordination_runtime import CoordinationLeaseLost
from app.services.review.review_run_budget import AttemptState, ReviewRepositoryRunBudgetStore, ReviewRunBudget
from app.services.review.runtime.review_runtime import TaskReviewRuntime
from app.services.review.task_context_pages import build_context_pages
from app.services.review.task_runtime_checkpoint import TaskRuntimeCheckpoint
from tests.support.persistence import persistence as persistence
from tests.support.runtime_harness import make_slice


def submit(payload):
    return {"kind": "submit", "examinations": [{"target_id": goal["goal_id"],
        "disposition": "examined", "evidence_ids": payload["active_evidence_ids"],
        "reason": "检查完整范围"} for goal in payload["goals"]],
        "candidates": [{"claim": "候选主张", "primary_anchor": "t1",
                        "evidence_ids": payload["active_evidence_ids"]}], "leads": []}


async def configured(tmp_path, persistence, *, responses=None, restore=False):
    repository, _, _ = persistence
    if not restore:
        repository.create_task(ReviewTask(id="run", pr_url="https://github.com/a/b/pull/1",
                                         engine="task_review_v1"))
    tmp_path.mkdir(parents=True)
    step, messages = make_slice(tmp_path, responses if responses is not None else [submit])
    step.executor.work_item.unit_id = "u1"
    store = ReviewRepositoryRunBudgetStore(repository)
    step.budget = await ReviewRunBudget.load_or_create("run", max_tokens=100_000, max_calls=5,
                                                    store=store, recover_running=False)
    runtime = TaskReviewRuntime([step.executor.work_item])
    checkpoint = TaskRuntimeCheckpoint(runtime, {"w1": step}, repository)
    await checkpoint.restore()
    return runtime, checkpoint, step, messages


async def test_connection_failure_persists_safe_usage_and_keeps_unknown_attempts(tmp_path, persistence):
    import httpx
    from openai import APIConnectionError
    from app.services.review.task_review_projection import project_task_runtime
    runtime, _, step, messages = await configured(tmp_path / "first", persistence)
    step.provider._request_attempts = 2
    step.provider._retry_backoff_seconds = 0
    class SDK:
        async def ainvoke(self, payload):
            messages.append(payload)
            raise APIConnectionError(request=httpx.Request("POST", "https://example.test/secret")) from httpx.ConnectError(
                "https://proxy-user:proxy-secret@example.test api-key=private")
    step.provider._build_chat_model = lambda *args: SDK()
    assert await runtime.run({"w1": step}, deadline_seconds=10) == "technical_failure"
    report = project_task_runtime(runtime, {"w1": step}, list(step.executor.goals.values()),
                                  termination_reason="technical_failure")
    assert report.execution_status == "failed" and report.usage_integrity == "unknown"
    assert report.unknown_attempts == 2 and report.sent_calls == 2 and report.total_tokens is None
    assert "model_connection_error" in report.failure_summary
    assert "尝试 2 次" in report.failure_summary
    assert runtime.failures[0]["work_item_id"] == "w1"
    failure = runtime.failures[0]["provider_failure"]
    assert failure["cause_types"] == ["APIConnectionError", "ConnectError"]
    assert len(step.usages) == 1 and not step.usages[0].usage_available
    assert step.usages[0].response_metadata == {"provider_failure": failure}
    restored, _, restored_step, new_messages = await configured(tmp_path / "second", persistence, restore=True)
    assert restored_step.usages == step.usages
    assert restored_step.budget.unknown_reserved > 0
    assert all(not attempt.usage_available for attempt in restored_step.budget.attempts.values())
    assert await restored.advance_one({"w1": restored_step}) is None and not new_messages
    exported = json.dumps([report.model_dump(mode="json"), [usage.model_dump(mode="json") for usage in restored_step.usages]])
    assert "proxy-secret" not in exported and "proxy-user" not in exported and "private" not in exported


@pytest.mark.parametrize("boundary", ["reserve", "dispatch", "settled", "applied", "response_before_settle"])
async def test_crash_boundaries_reconcile_without_duplicate_send(tmp_path, persistence, boundary):
    runtime, checkpoint, step, messages = await configured(tmp_path / "first", persistence)
    if boundary == "reserve":
        async def crash(*args):
            raise asyncio.CancelledError()
        checkpoint.bind_attempt = crash
    elif boundary == "dispatch":
        class CancelledSDK:
            async def ainvoke(self, payload):
                messages.append(payload)
                raise asyncio.CancelledError()
        step.provider._build_chat_model = lambda *args: CancelledSDK()
    elif boundary == "settled":
        def crash(*args):
            raise asyncio.CancelledError()
        step.apply_response = crash
    elif boundary == "applied":
        original = checkpoint.applied
        async def crash(*args):
            await original(*args)
            raise asyncio.CancelledError()
        checkpoint.applied = crash
    else:
        original = checkpoint.save_response
        async def crash(*args):
            await original(*args)
            raise asyncio.CancelledError()
        checkpoint.save_response = crash
    with pytest.raises(asyncio.CancelledError):
        await runtime.advance_one({"w1": step})
    restored, _, restored_step, new_messages = await configured(tmp_path / "second", persistence, restore=True)
    budget = restored_step.budget
    item = restored_step.executor.work_item
    assert not new_messages
    if boundary == "reserve":
        assert len(messages) == 0 and budget.calls_consumed == 0
        assert item.state.value == "ready"
        assert all(a.state == AttemptState.RELEASED for a in budget.attempts.values())
    elif boundary == "dispatch":
        assert budget.unknown_reserved > 0 and budget.settled_actual == 0
        assert item.state.value == "unknown"
        assert await restored.retry_unit("u1", {"w1": restored_step}) == []
        assert await restored.advance_one({"w1": restored_step}) is None
    else:
        assert budget.settled_actual == 15 and budget.calls_consumed == 1
        assert item.state.value == "completed"
        assert len(restored_step.executor.projection.candidates) == 1
        assert await restored.advance_one({"w1": restored_step}) is None
        again, _, again_step, again_messages = await configured(tmp_path / "third", persistence, restore=True)
        assert again_step.budget.settled_actual == 15
        assert len(again_step.executor.projection.candidates) == 1 and not again_messages
        assert not again.ready()


async def test_settled_missing_response_keeps_cost_and_retry_uses_new_admission(tmp_path, persistence):
    runtime, checkpoint, step, _ = await configured(tmp_path / "first", persistence)
    def crash(*args):
        raise asyncio.CancelledError()
    step.apply_response = crash
    with pytest.raises(asyncio.CancelledError):
        await runtime.advance_one({"w1": step})
    reference = next(iter(checkpoint.data["calls"].values()))["response"]
    path = persistence[0]._artifacts.root / "run"
    next(path.glob("task-review-v1-response-*.txt")).unlink()
    runtime, _, step, messages = await configured(tmp_path / "second", persistence, restore=True)
    assert step.executor.work_item.state.value == "result_missing"
    assert step.budget.settled_actual == 15 and not messages
    old_attempts = set(step.budget.attempts)
    assert await runtime.retry_unit("u1", {"w1": step}) == ["w1"]
    await runtime.advance_one({"w1": step})
    assert step.budget.settled_actual == 30 and len(messages) == 1
    assert len(set(step.budget.attempts) - old_attempts) == 1 and reference
    again, again_checkpoint, again_step, again_messages = await configured(tmp_path / "third", persistence, restore=True)
    assert again_step.executor.work_item.state.value == "completed"
    assert not again_step.executor.projection.pending_target_ids
    assert len(again_step.executor.projection.accepted_target_ids) == 1
    assert again_step.budget.settled_actual == 30 and not again_messages
    assert await again.advance_one({"w1": again_step}) is None
    old_call = next(call for key, call in again_checkpoint.data["calls"].items() if key in old_attempts)
    assert old_call["applied"] is False and old_call.get("lost_response")
    assert old_call["superseded_by_attempt_id"] in set(again_step.budget.attempts) - old_attempts
    final, _, final_step, final_messages = await configured(tmp_path / "fourth", persistence, restore=True)
    assert final_step.executor.work_item.state.value == "completed" and not final_messages
    assert final_step.budget.settled_actual == 30 and not final.ready()


async def test_v5_candidate_correction_survives_database_restore_without_rechecking(tmp_path, persistence):
    from app.services.review.runtime.review_task_protocol import TaskReviewV1Protocol
    repository = persistence[0]
    repository.create_task(ReviewTask(id="run", pr_url="https://github.com/a/b/pull/1", engine="task_review_v1"))
    invalid = {"kind": "submit", "examinations": [{"target_id": "t1", "disposition": "examined",
        "evidence_ids": ["e1"], "reason": "已经检查"}],
        "candidates": [{"claim": "候选", "primary_anchor": "t1", "evidence_ids": ["e1"], "extra": None}]}
    corrected = {"kind": "submit", "candidates": [{"claim": "候选", "primary_anchor": "t1",
                                                       "evidence_ids": ["e1"]}]}
    async def configure(name, responses):
        folder = tmp_path / name
        folder.mkdir()
        step, messages = make_slice(folder, responses)
        step.executor.spec = step.executor.spec.model_copy(update={"prompt_version": "task-review-prompt-v5"})
        step.protocol = TaskReviewV1Protocol(step.executor.spec)
        step.budget = await ReviewRunBudget.load_or_create("run", max_tokens=100_000, max_calls=5,
            store=ReviewRepositoryRunBudgetStore(repository), recover_running=False)
        runtime = TaskReviewRuntime([step.executor.work_item])
        checkpoint = TaskRuntimeCheckpoint(runtime, {"w1": step}, repository)
        await checkpoint.restore()
        return runtime, step, messages
    runtime, step, messages = await configure("first", [invalid])
    await runtime.advance_one({"w1": step})
    result_id = step.executor.projection.examinations["t1"].result_id
    assert step.executor.work_item.format_corrections == 1 and len(messages) == 1
    restored, restored_step, restored_messages = await configure("second", [corrected])
    assert restored_step.rejected_items[0]["item"]["extra"] is None
    assert restored_step.executor.work_item.format_corrections == 1 and not restored_messages
    await restored.advance_one({"w1": restored_step})
    assert restored_step.executor.projection.examinations["t1"].result_id == result_id
    assert len(restored_step.executor.projection.candidates) == 1
    assert restored_step.budget.calls_consumed == 2 and restored_step.budget.settled_actual == 30
    assert restored_step.executor.work_item.state.value == "completed"
    assert len(restored_messages) == 1


async def test_real_sdk_budget_deferral_only_wakes_on_revision_and_does_not_split(tmp_path):
    step, messages = make_slice(tmp_path, [submit])
    obligation = await step.budget.create_obligation(owner_task_id="other", token_budget=100_000,
                                                   reason="necessary evidence")
    runtime = TaskReviewRuntime([step.executor.work_item])
    assert (await runtime.advance_round({"w1": step}))[0]["action"] == "deferred"
    assert not messages and not step.executor.pages
    attempts = step.executor.work_item.attempt_count
    assert await runtime.advance_round({"w1": step}) == []
    assert step.executor.work_item.attempt_count == attempts
    await step.budget.release_obligation(obligation.obligation_id)
    await runtime.advance_round({"w1": step})
    assert len(messages) == 1 and step.executor.work_item.state.value == "completed"


def work(identity, unit, kind="basic", sequence=0):
    return ReviewWorkItem(work_item_id=identity, unit_id=unit, sequence=sequence,
                          scheduling_class=kind, kind="inspect", goal_ids=[identity])


def test_unit_and_class_rotation_with_single_writer_capacity():
    runtime = TaskReviewRuntime([work("a", "u1"), work("b", "u1"), work("c", "u2"),
                                work("v", "u1", "verification"), work("i", "u1", "investigation")],
                               concurrency=2)
    assert runtime.claim_next().work_item_id == "a"
    assert runtime.claim_next().work_item_id == "c"
    with pytest.raises(RuntimeError, match="single writer"):
        runtime.claim_next()
    runtime.finish("a", state="ready")
    runtime.finish("c", state="ready")
    assert runtime.claim_next().work_item_id == "v"
    runtime.finish("v", state="ready")
    assert runtime.claim_next().work_item_id == "c"
    runtime.finish("c", state="ready")
    # 调查没有越过从未获得基础检查机会的 b；同类轮转也不会总领取 a。
    assert runtime.claim_next().work_item_id == "b"


async def test_concurrent_round_never_runs_one_work_item_twice_and_has_shared_budget(tmp_path):
    step, _ = make_slice(tmp_path, [submit])
    runtime = TaskReviewRuntime([step.executor.work_item], concurrency=4)
    first, second = await asyncio.gather(runtime.advance_round({"w1": step}),
                                         runtime.advance_round({"w1": step}))
    assert sorted([len(first), len(second)]) == [0, 1]
    assert step.budget.calls_consumed == 1


async def test_cancel_during_request_stops_new_sends_but_keeps_late_result(tmp_path, persistence):
    runtime, checkpoint, step, messages = await configured(tmp_path / "first", persistence)
    entered, release = asyncio.Event(), asyncio.Event()
    original = step.provider._build_chat_model
    sdk = original("fixture", 4096)
    class WaitingSDK:
        async def ainvoke(self, payload):
            entered.set()
            await release.wait()
            return await sdk.ainvoke(payload)
    step.provider._build_chat_model = lambda *args: WaitingSDK()
    obligation = await step.budget.create_obligation(owner_task_id="later", token_budget=20,
                                                   reason="closing")
    running = asyncio.create_task(runtime.advance_one({"w1": step}))
    await entered.wait()
    await runtime.cancel({"w1": step})
    release.set()
    await running
    assert len(messages) == 1 and step.budget.settled_actual == 15
    assert step.budget.obligations[obligation.obligation_id].state.value == "abandoned"
    assert step.executor.projection.examinations and runtime.cancelled
    assert await runtime.advance_round({"w1": step}) == []
    assert checkpoint.data["cancelled"]


async def test_lost_lease_blocks_writes_and_no_sdk_send(tmp_path, persistence):
    runtime, checkpoint, step, messages = await configured(tmp_path / "first", persistence)
    repository, _, sessions = persistence
    with sessions.begin() as session:
        session.add(WorkerJobOrm(id="job", task_id="run", kind="review", status="leased",
            idempotency_key="job-key", lease_owner="worker", attempts=1,
            lease_until=utcnow() + timedelta(seconds=30)))
    checkpoint.lease = {"job_id": "job", "owner": "worker", "attempt": 1}
    await checkpoint.persist()
    with sessions.begin() as session:
        session.get(WorkerJobOrm, "job").lease_owner = "other"
    with pytest.raises(CoordinationLeaseLost):
        await runtime.advance_one({"w1": step})
    assert checkpoint.invalidated and not messages
    with pytest.raises(CoordinationLeaseLost, match="reload"):
        await checkpoint.persist()
    assert repository.load_task_review_runtime("run")


async def test_cas_namespace_and_hash_artifacts(tmp_path, persistence):
    runtime, checkpoint, step, _ = await configured(tmp_path / "first", persistence)
    repository, _, sessions = persistence
    with sessions() as session:
        row = session.get(SideEffectOrm, "task-review-v1:run")
        assert row.effect_type == "task-review-runtime-v1"
        assert "return value" not in json.dumps(row.result)
        assert list(session.scalars(select(ArtifactOrm)))
    stale = deepcopy(checkpoint.data)
    await checkpoint.persist()
    with pytest.raises(CoordinationLeaseLost, match="revision"):
        repository.save_task_review_runtime("run", stale, stale["revision"] - 1)
    session_ref = checkpoint.data["sessions"]["w1"]
    content = repository._artifacts.read_text(session_ref["uri"])
    assert hashlib.sha256(content.encode()).hexdigest() == session_ref["sha256"]
    assert runtime.ready() and step


async def test_capacity_pages_retain_original_goal_and_aggregate_only_after_all_pages(tmp_path):
    step, messages = make_slice(tmp_path, [submit, submit, submit])
    executor = step.executor
    executor.goals["t1"] = executor.goals["t1"].model_copy(update={"end_line": 3})
    content = "first\nsecond\nthird"
    executor.evidence_catalog["e1"].update(content=content, end_line=3,
        content_hash=hashlib.sha256(content.encode()).hexdigest(), hunk_id="original-hunk")
    pages = build_context_pages(executor.work_item, list(executor.goals.values()),
                                 executor.evidence_catalog, capacity_chars=6)
    assert len(pages) == 3 and all(p.ranges[0]["hunk_id"] == "original-hunk" for p in pages)
    executor.bind_pages(pages)
    runtime = TaskReviewRuntime([executor.work_item])
    for index in range(3):
        await runtime.advance_one({"w1": step})
        assert len(executor.goals) == 1
        assert bool(executor.projection.examinations) == (index == 2)
    assert len(messages) == 3 and step.budget.calls_consumed == 3
    assert executor.work_item.state.value == "completed"
    assert len(executor.projection.examinations["t1"].evidence_ids) == 3
    assert all(json.loads(m[1].content)["goals"][0]["goal_id"] == "t1" for m in messages)


def test_invalid_page_manifest_cannot_shrink_denominator(tmp_path):
    step, _ = make_slice(tmp_path, [])
    executor = step.executor
    pages = build_context_pages(executor.work_item, list(executor.goals.values()),
                                 executor.evidence_catalog, capacity_chars=100)
    with pytest.raises(ValueError, match="incomplete"):
        executor.bind_pages([])
    broken = deepcopy(asdict(pages[0]))
    broken["ranges"][0]["original_end_line"] = 0
    from app.services.review.task_context_pages import ContextPage
    with pytest.raises(ValueError, match="invalid page"):
        executor.bind_pages([ContextPage(**broken)])


async def test_two_tasks_run_concurrently_with_one_budget_and_no_private_loop(tmp_path):
    first_root, second_root = tmp_path / "first", tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first, _ = make_slice(first_root, [submit])
    second, _ = make_slice(second_root, [submit])
    second.executor.work_item.work_item_id = "w2"
    second.scope.review_unit_id = "w2"
    second.budget = first.budget
    runtime = TaskReviewRuntime([first.executor.work_item, second.executor.work_item], concurrency=2)
    entered = 0
    released = asyncio.Event()
    for step in (first, second):
        sdk = step.provider._build_chat_model("fixture", 4096)
        class ConcurrentSDK:
            def __init__(self, original):
                self.original = original
            async def ainvoke(self, payload):
                nonlocal entered
                entered += 1
                if entered == 2:
                    released.set()
                await asyncio.wait_for(released.wait(), timeout=2)
                return await self.original.ainvoke(payload)
        step.provider._build_chat_model = lambda *args, sdk=sdk: ConcurrentSDK(sdk)
    observations = await runtime.advance_round({"w1": first, "w2": second})
    assert len(observations) == 2 and entered == 2
    assert first.budget.calls_consumed == 2 and first.budget.settled_actual == 30
    assert not runtime.ready() and not runtime._running


async def test_page_progress_and_frozen_correction_survive_restart(tmp_path, persistence):
    runtime, checkpoint, step, _ = await configured(tmp_path / "first", persistence,
                                                    responses=[submit, submit])
    # 初次配置需要在第一次持久化之前冻结页面；这里用同一个尚未发起请求的逻辑任务。
    executor = step.executor
    executor.goals["t1"] = executor.goals["t1"].model_copy(update={"end_line": 2})
    content = "first\nsecond"
    executor.evidence_catalog["e1"].update(content=content, end_line=2,
        content_hash=hashlib.sha256(content.encode()).hexdigest())
    pages = build_context_pages(executor.work_item, list(executor.goals.values()),
                                 executor.evidence_catalog, capacity_chars=6)
    executor.bind_pages(pages)
    # 新的 run 尚未执行：换独立 run 身份，防止改写已经持久化的冻结 manifest。
    executor.spec = executor.spec.model_copy(update={"run_id": "page-run"})
    repository = persistence[0]
    repository.create_task(ReviewTask(id="page-run", pr_url="https://github.com/a/b/pull/2", engine="task_review_v1"))
    step.budget = await ReviewRunBudget.load_or_create("page-run", max_tokens=100_000, max_calls=5,
        store=ReviewRepositoryRunBudgetStore(repository))
    from app.services.review.runtime.review_task_protocol import TaskReviewV1Protocol
    step.protocol = TaskReviewV1Protocol(executor.spec)
    checkpoint = TaskRuntimeCheckpoint(runtime, {"w1": step}, repository)
    await checkpoint.restore()
    executor.work_item.format_corrections = 1
    await runtime.advance_one({"w1": step})
    assert executor.page_index == 1 and not executor.projection.examinations
    executor.page_index = 0
    executor.page_examinations = {}
    executor.work_item.format_corrections = 0
    executor.work_item.attempt_count = 0
    step.budget = await ReviewRunBudget.load_or_create("page-run", max_tokens=0, max_calls=0,
        store=ReviewRepositoryRunBudgetStore(repository), recover_running=False)
    checkpoint = TaskRuntimeCheckpoint(runtime, {"w1": step}, repository)
    await checkpoint.restore()
    assert executor.page_index == 1 and executor.work_item.format_corrections == 1
    await runtime.advance_one({"w1": step})
    assert executor.projection.examinations and step.budget.settled_actual == 30


async def test_repeated_observation_stops_read_loop_without_resetting_budget(tmp_path):
    read = {"kind": "read", "tool": "file_read", "arguments": {
        "file_path": "src/a.py", "start_line": 1, "end_line": 1}, "purpose": "核验"}
    step, messages = make_slice(tmp_path, [read, read, read])
    runtime = TaskReviewRuntime([step.executor.work_item])
    assert await runtime.run({"w1": step}, deadline_seconds=5) == "no_progress"
    assert len(messages) == 3 and step.budget.settled_actual == 45
    assert step.executor.work_item.reason == "repeated_read_no_progress"
    assert step.executor.work_item.state.value == "waiting_evidence"


async def test_runtime_deadline_preserves_unknown_inflight_usage(tmp_path):
    step, _ = make_slice(tmp_path, [submit])
    entered = asyncio.Event()
    class SlowSDK:
        async def ainvoke(self, payload):
            entered.set()
            await asyncio.Event().wait()
    step.provider._build_chat_model = lambda *args: SlowSDK()
    runtime = TaskReviewRuntime([step.executor.work_item])
    assert await runtime.run({"w1": step}, deadline_seconds=2) == "deadline"
    assert entered.is_set() and step.budget.unknown_reserved > 0
    assert runtime.cancelled and await runtime.advance_round({"w1": step}) == []


def test_artifact_uri_with_spaces_and_invalid_kind_stays_controlled(tmp_path):
    from app.services.review.artifact_store import LocalArtifactStore
    store = LocalArtifactStore(tmp_path / "space directory")
    ref = store.put_text(task_id="run", kind="response", content="完整响应")
    assert store.read_text(ref.uri) == "完整响应"
    with pytest.raises(ValueError, match="kind"):
        store.put_text(task_id="run", kind="../outside", content="拒绝")


async def test_retry_keeps_valid_goals_budget_and_format_correction(tmp_path):
    step, _ = make_slice(tmp_path, [])
    executor = step.executor
    goal = ReviewGoal(goal_id="t2", unit_id="u1", kind="inspect", target_ref="src/a.py")
    executor.goals["t2"] = goal
    executor.work_item.goal_ids.append("t2")
    executor.work_item.unit_id = "u1"
    executor.projection.pending_target_ids.add("t2")
    executor.work_item.format_corrections = 1
    executor.parse_and_apply({"kind": "submit", "examinations": [{"target_id": "t1",
        "disposition": "examined", "evidence_ids": ["e1"], "reason": "已检查"}]})
    budget = step.budget.snapshot()
    runtime = TaskReviewRuntime([executor.work_item])
    assert await runtime.retry_unit("u1", {"w1": step}) == ["w1"]
    assert executor.work_item.format_corrections == 1 and step.budget.snapshot() == budget
    assert list(executor.projection.examinations) == ["t1"]
    assert [goal.goal_id for goal in executor.current_goals()] == ["t2"]
