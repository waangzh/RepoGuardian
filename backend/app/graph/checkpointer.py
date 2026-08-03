"""LangGraph SQLite checkpointer 的进程生命周期管理。

业务状态由 SQLAlchemy 数据库负责，LangGraph saver 专门保存节点级恢复状态。每个 task 使用稳定
``thread_id``，Unit 子图使用独立 ``checkpoint_ns``。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.core.config import settings

_checkpointer: AsyncSqliteSaver | None = None
_connection: aiosqlite.Connection | None = None
_lock = asyncio.Lock()


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
