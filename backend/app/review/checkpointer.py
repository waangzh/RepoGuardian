"""LangGraph SQLite checkpointer 的进程生命周期管理。

业务状态由 SQLAlchemy 数据库负责，LangGraph saver 专门保存节点级恢复状态。每个 task 使用稳定
``thread_id``，旧运行数据不兼容当前审查图。
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
from app.review.models import BatchVerificationOutput, DiscoveryOutput, VerificationOutput, VerificationResult

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
    """父图只保存原生数据，拒绝任意额外模块构造器与 pickle。"""
    return JsonPlusSerializer(allowed_msgpack_modules=[
        (model.__module__, model.__name__) for model in (
            DiscoveryOutput, VerificationOutput, BatchVerificationOutput, VerificationResult)
    ])



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
    """删除任务的唯一图执行状态。"""
    checkpointer = await get_checkpointer()
    await checkpointer.adelete_thread(thread_id)


async def list_checkpoint_thread_ids() -> list[str]:
    """列出仍有主图恢复状态的任务 thread。"""
    checkpointer = await get_checkpointer()
    assert _connection is not None
    async with checkpointer.lock:
        async with _connection.execute(
            "SELECT thread_id FROM checkpoints UNION SELECT thread_id FROM writes"
        ) as cursor:
            rows = await cursor.fetchall()
    return list(dict.fromkeys(str(row[0]) for row in rows))


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
    # LangGraph 根图执行实际保存到空 namespace；非空值会被 get_state 当作子图路径。
    configurable = {"thread_id": task_id, "checkpoint_ns": ""}
    if checkpoint_id:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}
