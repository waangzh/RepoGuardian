"""LangGraph SQLite checkpointer 的进程生命周期管理。

业务状态由 SQLAlchemy 数据库负责，LangGraph saver 专门保存节点级恢复状态。每个 task 使用稳定
``thread_id``，Unit 子图使用独立 ``checkpoint_ns``。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

import aiosqlite
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.core.config import settings

_checkpointer: AsyncSqliteSaver | None = None
_connection: aiosqlite.Connection | None = None
_lock = asyncio.Lock()
logger = logging.getLogger("RepoGuardian.Checkpointer")


@dataclass(frozen=True)
class CheckpointStorageStats:
    page_size: int
    page_count: int
    freelist_count: int

    @property
    def database_bytes(self) -> int:
        return self.page_size * self.page_count

    @property
    def reclaimable_bytes(self) -> int:
        return self.page_size * self.freelist_count

    @property
    def reclaimable_ratio(self) -> float:
        if self.page_count == 0:
            return 0.0
        return self.freelist_count / self.page_count


def _checkpoint_serializer() -> JsonPlusSerializer:
    """仅允许恢复审查状态中确实使用的自定义枚举类型。"""
    return JsonPlusSerializer(
        allowed_msgpack_modules=[("app.models.review", "ReviewPhase")]
    )


async def get_checkpointer() -> AsyncSqliteSaver:
    global _checkpointer, _connection
    if _checkpointer is not None:
        return _checkpointer
    async with _lock:
        if _checkpointer is None:
            path = Path(settings.repoguardian_checkpoint_db).resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            _connection = await aiosqlite.connect(path.as_posix())
            _checkpointer = AsyncSqliteSaver(
                _connection,
                serde=_checkpoint_serializer(),
            )
            await _checkpointer.setup()
    return _checkpointer


async def close_checkpointer() -> None:
    global _checkpointer, _connection
    async with _lock:
        if _connection is not None:
            await _connection.close()
        _connection = None
        _checkpointer = None


async def delete_thread_checkpoints(thread_id: str) -> None:
    """删除一个任务在所有 checkpoint namespace 下的恢复状态。"""
    checkpointer = await get_checkpointer()
    await checkpointer.adelete_thread(thread_id)


async def list_checkpoint_thread_ids() -> list[str]:
    """列出 checkpoint 数据库中实际仍有恢复状态的 thread。"""
    checkpointer = await get_checkpointer()
    assert _connection is not None
    async with checkpointer.lock:
        async with _connection.execute(
            "SELECT thread_id FROM checkpoints UNION SELECT thread_id FROM writes"
        ) as cursor:
            rows = await cursor.fetchall()
    return [str(row[0]) for row in rows]


async def checkpoint_storage_stats() -> CheckpointStorageStats:
    """读取 checkpoint SQLite 的页使用情况。"""
    checkpointer = await get_checkpointer()
    assert _connection is not None
    async with checkpointer.lock:
        values: list[int] = []
        for pragma in ("page_size", "page_count", "freelist_count"):
            async with _connection.execute(f"PRAGMA {pragma}") as cursor:
                row = await cursor.fetchone()
                values.append(int(row[0]) if row else 0)
    return CheckpointStorageStats(*values)


async def compact_checkpoints_if_needed(
    *, min_reclaim_bytes: int, min_reclaim_ratio: float
) -> bool:
    """仅在释放页达到阈值时 checkpoint WAL 并执行 VACUUM。"""
    checkpointer = await get_checkpointer()
    assert _connection is not None
    async with checkpointer.lock:
        values: list[int] = []
        for pragma in ("page_size", "page_count", "freelist_count"):
            async with _connection.execute(f"PRAGMA {pragma}") as cursor:
                row = await cursor.fetchone()
                values.append(int(row[0]) if row else 0)
        stats = CheckpointStorageStats(*values)
        if (
            stats.reclaimable_bytes < min_reclaim_bytes
            or stats.reclaimable_ratio < min_reclaim_ratio
        ):
            return False
        logger.info(
            "开始压缩 checkpoint 数据库，可回收 %.2f MiB（%.1f%%）",
            stats.reclaimable_bytes / (1024 * 1024),
            stats.reclaimable_ratio * 100,
        )
        async with _connection.execute("PRAGMA wal_checkpoint(TRUNCATE)"):
            pass
        async with _connection.execute("VACUUM"):
            pass
        return True


def review_thread_config(task_id: str, *, checkpoint_id: str | None = None) -> dict:
    configurable = {"thread_id": task_id, "checkpoint_ns": "review"}
    if checkpoint_id:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


def unit_thread_config(task_id: str, unit_id: str) -> dict:
    return {
        "configurable": {
            "thread_id": task_id,
            "checkpoint_ns": f"unit:{unit_id}",
        }
    }
