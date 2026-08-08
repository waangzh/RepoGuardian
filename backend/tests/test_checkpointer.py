from pathlib import Path

import pytest

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
)
from app.models.review import ReviewPhase


def test_checkpoint_serializer_allows_review_phase_explicitly() -> None:
    serializer = _checkpoint_serializer()

    encoded = serializer.dumps_typed(ReviewPhase.prepare)

    assert serializer.loads_typed(encoded) == ReviewPhase.prepare


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
        ],
    )
    await connection.execute(
        "INSERT INTO writes "
        "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("task-1", "review", "cp-1", "node", 0, "state", "msgpack", b"value"),
    )
    await connection.commit()

    assert set(await list_checkpoint_thread_ids()) == {"task-1", "task-2"}

    await delete_thread_checkpoints("task-1")

    async with connection.execute(
        "SELECT COUNT(*) FROM checkpoints WHERE thread_id = 'task-1'"
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
    del saver
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
