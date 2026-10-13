"""LangGraph SQLite checkpointer 的进程生命周期管理。

业务状态由 SQLAlchemy 数据库负责，LangGraph saver 专门保存节点级恢复状态。每个 task 使用稳定
``thread_id``，独立调用的 Unit 图使用派生 thread，避免根图重置 namespace 后共享状态。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import aiosqlite
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import BaseModel

import app.models.review as review_models
from app.core.config import settings
from app.services.fingerprints import DIAGNOSIS_INPUT_VERSION

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
    """仅允许恢复本项目 review 模型，拒绝任意外部模块构造器。"""
    allowed = [
        (review_models.__name__, name)
        for name, value in inspect.getmembers(review_models, inspect.isclass)
        if value.__module__ == review_models.__name__
        and (issubclass(value, BaseModel) or issubclass(value, Enum))
    ]
    return JsonPlusSerializer(
        allowed_msgpack_modules=allowed
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
    """删除任务根图及其所有 Unit/批次的恢复状态。"""
    checkpointer = await get_checkpointer()
    assert _connection is not None
    prefix = f"{thread_id}:unit:"
    async with checkpointer.lock:
        async with _connection.execute(
            "SELECT thread_id FROM checkpoints UNION SELECT thread_id FROM writes"
        ) as cursor:
            rows = await cursor.fetchall()
    owned = {thread_id, *(str(row[0]) for row in rows if str(row[0]).startswith(prefix))}
    for owned_thread in owned:
        await checkpointer.adelete_thread(owned_thread)


async def list_checkpoint_thread_ids() -> list[str]:
    """列出仍有根图或 Unit 恢复状态的任务 thread，供任务保留期 GC 使用。"""
    checkpointer = await get_checkpointer()
    assert _connection is not None
    async with checkpointer.lock:
        async with _connection.execute(
            "SELECT thread_id FROM checkpoints UNION SELECT thread_id FROM writes"
        ) as cursor:
            rows = await cursor.fetchall()
    return list(dict.fromkeys(str(row[0]).split(":unit:", 1)[0] for row in rows))


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
    # LangGraph 根图执行实际保存到空 namespace；非空值会被 get_state 当作子图路径。
    configurable = {"thread_id": task_id, "checkpoint_ns": ""}
    if checkpoint_id:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


def unit_thread_config(task_id: str, unit_id: str) -> dict:
    # 独立 ainvoke 的根图会清空 checkpoint_ns；隔离必须建立在 thread_id 上。
    identity = hashlib.sha256(f"{unit_id}:{DIAGNOSIS_INPUT_VERSION}".encode()).hexdigest()
    return {
        "configurable": {
            "thread_id": f"{task_id}:unit:{identity}",
            "checkpoint_ns": "",
        }
    }
