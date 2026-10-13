"""发送前检查点故障注入；SDK stub 不执行目标代码。"""

import asyncio

import pytest

from app.services.review_run_budget import AttemptState
from test_phase6a_persistence import persistence as persistence
from test_task_runtime_p2 import configured


@pytest.mark.parametrize("guard_number", [1, 2])
async def test_cancel_while_dispatch_intent_persists_reconciles_before_restart(tmp_path, persistence, guard_number):
    runtime, checkpoint, step, messages = await configured(tmp_path / "first", persistence)
    original = checkpoint._persist_locked
    calls = 0

    async def inject():
        nonlocal calls
        await original()
        if any(call["state"] == "dispatch_intent" for call in checkpoint.data["calls"].values()):
            calls += 1
            if calls == guard_number:
                # cancel() 先写内存标记，再等待同一个 checkpoint 锁；模拟这个精确边界。
                runtime.cancelled = True

    checkpoint._persist_locked = inject
    with pytest.raises(asyncio.CancelledError):
        await runtime.advance_one({"w1": step})
    assert not messages
    attempt = next(iter(step.budget.attempts.values()))
    assert attempt.state == (AttemptState.RELEASED if guard_number == 1 else AttemptState.UNKNOWN)
    assert step.budget.in_flight_reserved == 0
    assert step.budget.calls_consumed == (0 if guard_number == 1 else 1)
    assert (step.budget.unknown_reserved > 0) == (guard_number == 2)
    checkpoint._persist_locked = original
    await runtime.cancel({"w1": step})
    restored, _, restored_step, new_messages = await configured(tmp_path / "second", persistence, restore=True)
    assert not new_messages and restored.cancelled
    assert await restored.advance_one({"w1": restored_step}) is None
    again = next(iter(restored_step.budget.attempts.values()))
    assert again.state == attempt.state
    assert restored_step.budget.calls_consumed == step.budget.calls_consumed


@pytest.mark.parametrize("boundary", ["checkpoint_before", "checkpoint_after", "dispatch_before", "dispatch_after"])
async def test_task_cancellation_at_persistence_boundary_never_replays_sdk(tmp_path, persistence, boundary):
    runtime, checkpoint, step, messages = await configured(tmp_path / "first", persistence)
    entered = asyncio.Event()
    gate = asyncio.Event()
    original_checkpoint = checkpoint._persist_locked
    original_budget = step.budget._persist

    async def checkpoint_injection():
        guarded = any(call["state"] == "dispatch_intent" for call in checkpoint.data["calls"].values())
        if guarded and boundary == "checkpoint_before":
            entered.set()
            await gate.wait()
        await original_checkpoint()
        if guarded and boundary == "checkpoint_after":
            entered.set()
            await gate.wait()

    async def budget_injection():
        guarded = any(attempt.state == AttemptState.DISPATCHING for attempt in step.budget.attempts.values())
        if guarded and boundary == "dispatch_before":
            entered.set()
            await gate.wait()
        await original_budget()
        if guarded and boundary == "dispatch_after":
            entered.set()
            await gate.wait()

    checkpoint._persist_locked = checkpoint_injection
    step.budget._persist = budget_injection
    task = asyncio.create_task(runtime.advance_one({"w1": step}))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not messages
    restored, _, restored_step, new_messages = await configured(tmp_path / "second", persistence, restore=True)
    attempt = next(iter(restored_step.budget.attempts.values()))
    expected = AttemptState.UNKNOWN if boundary == "dispatch_after" else AttemptState.RELEASED
    assert attempt.state == expected and restored_step.budget.in_flight_reserved == 0
    assert restored_step.budget.calls_consumed == (1 if expected == AttemptState.UNKNOWN else 0)
    if expected == AttemptState.UNKNOWN:
        assert restored_step.executor.work_item.state.value == "unknown"
    assert not new_messages
    # restore 只对账，不自行发送；调用方取消后不得开始新的逻辑请求。
    await restored.cancel({"w1": restored_step})
    assert await restored.advance_one({"w1": restored_step}) is None
