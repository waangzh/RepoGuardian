from pathlib import Path
from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph

from app.core.config import settings
from app.graph import checkpointer as checkpointer_module
from app.graph.checkpointer import (
    _checkpoint_serializer,
    checkpoint_storage_stats,
    close_checkpointer,
    compact_checkpoints_if_needed,
    delete_thread_checkpoints,
    get_checkpointer,
    list_checkpoint_thread_ids,
    review_thread_config,
    unit_thread_config,
)
from app.models.review import ReviewPhase, UnitInputCoverage
from app.services.review_unit_executor import ReviewUnitExecutor


def test_checkpoint_serializer_allows_review_phase_explicitly() -> None:
    serializer = _checkpoint_serializer()

    encoded = serializer.dumps_typed(ReviewPhase.prepare)

    assert serializer.loads_typed(encoded) == ReviewPhase.prepare


def test_checkpoint_serializer_restores_review_models() -> None:
    serializer = _checkpoint_serializer()
    original = UnitInputCoverage(
        evidence_coverage="complete",
        target_coverage="partial",
        contract_coverage="complete",
        omitted_targets=["tests/test_stats.py"],
    )

    restored = serializer.loads_typed(serializer.dumps_typed(original))

    assert isinstance(restored, UnitInputCoverage)
    assert restored.target_coverage == "partial"
    assert restored.omitted_targets == ["tests/test_stats.py"]


def test_unit_graph_state_normalizes_legacy_checkpoint_dicts() -> None:
    state = ReviewUnitExecutor._normalize_graph_state({
        "input_coverage": {
            "evidence_coverage": "complete",
            "target_coverage": "partial",
            "contract_coverage": "complete",
        },
        "plan_status": "skipped",
        "model_usages": [],
    })

    assert isinstance(state["input_coverage"], UnitInputCoverage)
    assert state["input_coverage"].target_coverage == "partial"


@pytest.mark.asyncio
async def test_delete_thread_removes_all_namespaces_and_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await close_checkpointer()
    monkeypatch.setattr(settings, "repoguardian_checkpoint_db", tmp_path / "checkpoints.db")
    saver = await get_checkpointer()
    assert checkpointer_module._connection is not None
    connection = checkpointer_module._connection
    await connection.executemany(
        "INSERT INTO checkpoints "
        "(thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("task-1", "review", "cp-1", "msgpack", b"one", b"{}"),
            ("task-1", "unit:1", "cp-2", "msgpack", b"two", b"{}"),
            ("task-2", "review", "cp-3", "msgpack", b"three", b"{}"),
            (unit_thread_config("task-1", "batch-1")["configurable"]["thread_id"],
             "", "cp-4", "msgpack", b"four", b"{}"),
            ("task-10", "", "cp-5", "msgpack", b"five", b"{}"),
        ],
    )
    await connection.execute(
        "INSERT INTO writes "
        "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("task-1", "review", "cp-1", "node", 0, "state", "msgpack", b"value"),
    )
    await connection.commit()

    assert set(await list_checkpoint_thread_ids()) == {"task-1", "task-2", "task-10"}

    await delete_thread_checkpoints("task-1")

    async with connection.execute(
        "SELECT COUNT(*) FROM checkpoints WHERE thread_id = 'task-1' "
        "OR thread_id LIKE 'task-1:unit:%'"
    ) as cursor:
        assert (await cursor.fetchone())[0] == 0
    async with connection.execute(
        "SELECT COUNT(*) FROM writes WHERE thread_id = 'task-1'"
    ) as cursor:
        assert (await cursor.fetchone())[0] == 0
    async with connection.execute(
        "SELECT COUNT(*) FROM checkpoints WHERE thread_id = 'task-2'"
    ) as cursor:
        assert (await cursor.fetchone())[0] == 1
    assert set(await list_checkpoint_thread_ids()) == {"task-2", "task-10"}
    del saver
    await close_checkpointer()


class _IsolationState(TypedDict, total=False):
    marker: str
    inherited: str


@pytest.mark.asyncio
async def test_unit_batches_isolate_nested_state_and_resume_from_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await close_checkpointer()
    monkeypatch.setattr(settings, "repoguardian_checkpoint_db", tmp_path / "isolation.db")
    saver = await get_checkpointer()
    observations = []

    async def inspect_batch(state):
        observations.append(dict(state))
        return {"inherited": state["marker"]}

    builder = StateGraph(_IsolationState)
    builder.add_node("inspect", inspect_batch)
    builder.add_edge(START, "inspect")
    builder.add_edge("inspect", END)
    child = builder.compile(checkpointer=saver)

    async def run_batches(_state):
        for batch in ("first", "second"):
            await child.ainvoke({"marker": batch}, unit_thread_config("task", batch))
        return {"inherited": "parent"}

    parent_builder = StateGraph(_IsolationState)
    parent_builder.add_node("batches", run_batches)
    parent_builder.add_edge(START, "batches")
    parent_builder.add_edge("batches", END)
    parent = parent_builder.compile(checkpointer=saver)
    try:
        await parent.ainvoke({"marker": "parent"}, review_thread_config("task"))
        assert observations == [{"marker": "first"}, {"marker": "second"}]
        parent_state = await parent.aget_state(review_thread_config("task"))
        assert parent_state.values == {"marker": "parent", "inherited": "parent"}
        assert await list_checkpoint_thread_ids() == ["task"]

        await close_checkpointer()
        reopened = builder.compile(checkpointer=await get_checkpointer())
        first = await reopened.aget_state(unit_thread_config("task", "first"))
        second = await reopened.aget_state(unit_thread_config("task", "second"))
        assert first.values == {"marker": "first", "inherited": "first"}
        assert second.values == {"marker": "second", "inherited": "second"}
        assert not (await reopened.aget_state(unit_thread_config("task", "third"))).values
        await delete_thread_checkpoints("task")
        assert await list_checkpoint_thread_ids() == []
    finally:
        await close_checkpointer()


@pytest.mark.asyncio
async def test_checkpoint_vacuum_runs_only_after_reclaim_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await close_checkpointer()
    db_path = tmp_path / "checkpoints.db"
    monkeypatch.setattr(settings, "repoguardian_checkpoint_db", db_path)
    await get_checkpointer()
    assert checkpointer_module._connection is not None
    connection = checkpointer_module._connection
    for index in range(8):
        await connection.execute(
            "INSERT INTO checkpoints "
            "(thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, metadata) "
            "VALUES (?, '', ?, 'msgpack', zeroblob(1048576), ?)",
            ("large-task", f"cp-{index}", b"{}"),
        )
    await connection.commit()
    await delete_thread_checkpoints("large-task")
    before = await checkpoint_storage_stats()
    assert before.reclaimable_bytes > 0

    assert not await compact_checkpoints_if_needed(
        min_reclaim_bytes=before.reclaimable_bytes + 1,
        min_reclaim_ratio=0.0,
    )
    assert await compact_checkpoints_if_needed(
        min_reclaim_bytes=1,
        min_reclaim_ratio=0.0,
    )
    after = await checkpoint_storage_stats()
    assert after.freelist_count == 0
    await close_checkpointer()
