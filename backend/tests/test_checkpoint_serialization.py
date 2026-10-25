from enum import Enum

import aiosqlite
import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import BaseModel

from app.graph.checkpointer import _checkpoint_serializer
from app.models.review import (
    ExecutionBudget,
    ReviewMode,
    ReviewPhase,
    ReviewTask,
    TaskStatus,
    ValidationBackend,
)


@pytest.mark.parametrize("legacy", [False, True], ids=["current-modules", "legacy-module"])
async def test_checkpoint_reopen_preserves_review_types(tmp_path, monkeypatch, legacy):
    database = tmp_path / "checkpoint.db"
    config = {"configurable": {"thread_id": "review", "checkpoint_ns": ""}}
    state = {
        "phase": ReviewPhase.prepare,
        "budget": ExecutionBudget(),
        "task": ReviewTask(id="saved", pr_url="https://github.com/local/repo/pull/1"),
    }
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = state
    checkpoint["channel_versions"] = {name: "1" for name in state}

    async with aiosqlite.connect(database) as connection:
        saver = AsyncSqliteSaver(connection, serde=_checkpoint_serializer())
        await saver.setup()
        with monkeypatch.context() as names:
            if legacy:
                for model in (
                    ExecutionBudget, ReviewMode, ReviewPhase, ReviewTask, TaskStatus,
                    ValidationBackend,
                ):
                    names.setattr(model, "__module__", "app.models.review")
            await saver.aput(
                config, checkpoint, {"source": "input", "step": 0, "parents": {}},
                checkpoint["channel_versions"],
            )

    async with aiosqlite.connect(database) as connection:
        saver = AsyncSqliteSaver(connection, serde=_checkpoint_serializer())
        saved = await saver.aget_tuple(config)
    assert saved is not None
    restored = saved.checkpoint["channel_values"]
    assert restored["phase"] is ReviewPhase.prepare
    assert isinstance(restored["budget"], ExecutionBudget)
    assert isinstance(restored["task"], ReviewTask)
    assert restored["task"].status is state["task"].status
    assert restored["budget"].model_dump() == state["budget"].model_dump()
    assert restored["task"].model_dump() == state["task"].model_dump()


class UntrustedCheckpointModel(BaseModel):
    value: str


class UntrustedCheckpointEnum(str, Enum):
    value = "untrusted"


@pytest.mark.parametrize("value", [
    UntrustedCheckpointModel(value="untrusted"),
    UntrustedCheckpointEnum.value,
])
def test_checkpoint_does_not_construct_unapproved_types(value):
    serializer = _checkpoint_serializer()
    restored = serializer.loads_typed(serializer.dumps_typed(value))
    assert not isinstance(restored, type(value))
    assert restored == ({"value": "untrusted"} if isinstance(value, BaseModel) else "untrusted")
