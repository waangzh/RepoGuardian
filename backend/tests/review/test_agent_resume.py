"""Restart a real agent graph after an abrupt exit between tools and the model."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import aiosqlite
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.review.agents import ReviewGuardMiddleware
from app.review.checkpointer import _checkpoint_serializer
from app.review.context import AgentContext, ReviewContext
from app.review.evidence import EvidenceService
from app.review.graph import build_review_graph
from app.review.models import ReviewSnapshot
from app.services.review.artifact_store import LocalArtifactStore
from app.tools.git_tool import GitTool
from tests.review.test_native_agents import ToolCallingModel
from tests.review.test_native_graph import CHANGE

pytest_plugins = ["tests.review.test_native_agents"]


def record(root, stage, kind, **data):
    with (root / "events.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps({"stage": stage, "kind": kind, **data}) + "\n")


class ResumeModel(ToolCallingModel):
    directory: str
    stage: str

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        payload = json.loads(next(message.content for message in messages if message.type == "human"))
        responses = [message for message in messages if isinstance(message, ToolMessage)]
        record(Path(self.directory), self.stage, "model", agent=payload["assignment"]["id"],
               tools=[message.tool_call_id for message in responses])
        if not responses:
            calls = [{"name": "read_file", "args": {"path": path, "start": 1, "end": 1}, "id": path}
                     for path in ("safe.py", "other.py")]
        elif len(responses) == 2:
            calls = [{"name": "read_base_file", "args": {"path": "safe.py", "start": 1, "end": 1},
                      "id": "base"}]
        else:
            evidence = [json.loads(message.content)["evidence"] for message in responses]
            calls = [{"name": "DiscoveryOutput", "id": "result", "args": {
                "findings": [], "examined": [
                    {"goal_id": goal["id"], "evidence_ids": [
                        item["id"] for item in evidence if item["path"] == goal["path"]
                        and item["side"] == goal["side"]]}
                    for goal in payload["goals"]
                ],
            }}]
        return ChatResult(generations=[ChatGeneration(message=AIMessage(
            content="", tool_calls=calls,
            usage_metadata={"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
        ))])


async def run_process(root, stage):
    source = EvidenceService(
        snapshot=ReviewSnapshot.model_validate_json((root / "snapshot.json").read_text(encoding="utf-8")),
        repo_path=root / "repo", allowed_paths=frozenset({"safe.py", "other.py"}),
        artifacts=LocalArtifactStore(root / "artifacts"), git=GitTool(),
    )
    original_read = source.read

    async def observed_read(*args, **kwargs):
        result = await original_read(*args, **kwargs)
        record(root, stage, "read", path=result["evidence"]["path"], side=result["evidence"]["side"])
        return result

    source.read = observed_read
    original_guard = ReviewGuardMiddleware.abefore_model

    async def crash_before_model(self, state, runtime):
        await original_guard(self, state, runtime)
        tools = [message for message in state["messages"]
                 if isinstance(message, ToolMessage) and message.name == "read_file"]
        if stage == "crash" and len(tools) == 2:
            payload = json.loads(next(message.content for message in state["messages"] if message.type == "human"))
            record(root, stage, "crash", agent=payload["assignment"]["id"])
            os._exit(77)

    ReviewGuardMiddleware.abefore_model = crash_before_model

    async def load():
        return AgentContext(evidence=source, changed_files=[CHANGE])

    config = {"configurable": {"thread_id": "review"}, "max_concurrency": 1}
    async with aiosqlite.connect(str(root / "checkpoints.sqlite")) as connection:
        saver = AsyncSqliteSaver(connection, serde=_checkpoint_serializer())
        await saver.setup()
        if stage == "resume":
            rows = await (await connection.execute(
                "SELECT DISTINCT checkpoint_ns FROM checkpoints WHERE checkpoint_ns != ''"
            )).fetchall()
            for row in rows:
                saved = await saver.aget_tuple({"configurable": {
                    "thread_id": "review", "checkpoint_ns": row[0],
                }})
                record(root, stage, "persisted", tools=[
                    message.tool_call_id for message in saved.checkpoint["channel_values"].get("messages", [])
                    if isinstance(message, ToolMessage)
                ])
        result = await build_review_graph(saver).ainvoke(
            {"review_id": "review"} if stage == "crash" else None, config,
            context=ReviewContext(load=load, model=ResumeModel(directory=str(root), stage=stage)),
            durability="sync",
        )
        record(root, stage, "done", complete=result["report"]["complete"],
               termination=result["report"]["termination"])


def test_sync_checkpoints_resume_tool_messages_without_repeating_completed_calls(source_service, tmp_path):
    (tmp_path / "snapshot.json").write_text(source_service.snapshot.model_dump_json(), encoding="utf-8")
    for stage, expected in (("crash", 77), ("resume", 0)):
        process = subprocess.run(
            [sys.executable, "-m", "tests.review.test_agent_resume", str(tmp_path), stage],
            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=45,
        )
        assert process.returncode == expected, process.stderr
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    interrupted = next(event["agent"] for event in events if event["kind"] == "crash")
    persisted = [event for event in events if event["kind"] == "persisted"]
    assert any(event["tools"] == ["safe.py", "other.py"] for event in persisted)
    calls = [event for event in events if event["kind"] == "model" and event["agent"] == interrupted]
    assert [event["stage"] for event in calls] == ["crash", "resume", "resume"]
    assert [event["tools"] for event in calls] == [[], ["safe.py", "other.py"], ["safe.py", "other.py", "base"]]
    reads = [event for event in events if event["kind"] == "read"]
    assert sum(event["stage"] == "crash" for event in reads) == 2
    assert len(reads) == 6  # Three reads per assignment; the interrupted reads are not repeated.
    assert len([event for event in events if event["kind"] == "model"]) == 6
    assert events[-1] == {"stage": "resume", "kind": "done", "complete": True, "termination": "completed"}


if __name__ == "__main__":
    asyncio.run(run_process(Path(sys.argv[1]), sys.argv[2]))
