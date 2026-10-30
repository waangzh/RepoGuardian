import json

import aiosqlite
import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.review.agents import ReviewGuardMiddleware
from app.review.checkpointer import _checkpoint_serializer
from app.review.context import AgentContext
from app.review.evidence import uncovered_ranges
from app.review.findings import covered_goals
from app.review.materials import prepare_materials
from app.review.models import DiscoveryOutput, ReviewGoal, ReviewLimits
from app.review.nodes.agent_execution import run_agent
from app.review.submission import submission_feedback
from app.review.tools import REVIEW_TOOLS, read_file
from tests.review.test_native_agents import ToolCallingModel
from tests.review.test_native_graph import review_context
from tests.review.test_retrieval import commit_sources
from tests.review.test_failure_recovery import tool_runtime

pytest_plugins = ["tests.review.test_native_agents"]


def goals():
    return [ReviewGoal(id=side, path="safe.py", side=side, start_line=1, end_line=1).model_dump()
            for side in ("head", "base")]


def coverage(service, expected, output, issued):
    return covered_goals(expected, [{
        "id": "assignment", "status": "completed", "output": output, "evidence_ids": issued,
    }], service, [{"id": "assignment", "goal_ids": [goal["id"] for goal in expected]}])


@pytest.mark.parametrize("failure,reason", [
    ("side", "侧别不匹配"), ("path", "文件与目标不匹配"), ("unissued", "未在当前分支读取"),
    ("missing", "未提交"),
])
async def test_rejected_goal_claim_has_an_actionable_reason(source_service, failure, reason):
    head = await source_service.read("safe.py", 1, 1)
    other = await source_service.read("other.py", 1, 1)
    identity = other["evidence"]["id"] if failure == "path" else head["evidence"]["id"]
    examined = [{"goal_id": "base", "evidence_ids": [identity]}] if failure != "missing" else []
    issued = [] if failure == "unissued" else [identity]
    result = coverage(source_service, [goals()[1]], {"examined": examined}, issued)
    assert result[0]["status"] == "not_examined"
    assert reason in result[0]["reason"]


async def test_structural_goal_coverage_requires_agent_read_evidence(source_service):
    goal = ReviewGoal(id="rename", kind="structural", path="safe.py", side="head",
                      description="检查旧路径的导入引用").model_dump()
    commit_sources(source_service, {"other.py": "from safe import VALUE\n"})
    other = await source_service.read("other.py", 1, 1)
    identity = other["evidence"]["id"]
    examined = [{"goal_id": "rename", "evidence_ids": [identity]}]
    result = coverage(source_service, [goal], {"examined": examined}, [identity])
    assert result[0]["status"] == "examined"
    assert result[0]["reason"] is None

    unissued = coverage(source_service, [goal], {"examined": examined}, [])
    assert unissued[0]["status"] == "not_examined"
    assert "未在当前分支读取" in unissued[0]["reason"]

    missing = coverage(source_service, [goal], {"examined": []}, [identity])
    assert missing[0]["status"] == "not_examined"
    assert "未提交该结构性检查结果" in missing[0]["reason"]


@pytest.mark.parametrize("goal_path,path,side", [
    ("safe.py", "safe.py", "head"),
    ("safe.py", "safe.py", "base"),
    ("safe.py", "other.py", "base"),
    ("removed.py", "removed.py", "base"),
    ("removed.py", "other.py", "base"),
])
async def test_structural_goal_rejects_target_and_base_only_evidence(
        source_service, goal_path, path, side):
    goal = ReviewGoal(id="structural", kind="structural", path=goal_path,
        side="base" if goal_path == "removed.py" else "head").model_dump()
    packet = await source_service.read(path, 1, 1, side)
    identity = packet["evidence"]["id"]
    output = DiscoveryOutput(examined=[{"goal_id": goal["id"], "evidence_ids": [identity]}])

    result = coverage(source_service, [goal], output.model_dump(), [identity])
    assert result[0]["status"] == "not_examined"
    assert "目标文件之外的 HEAD 调用方" in result[0]["reason"]
    feedback = submission_feedback(output, [goal], source_service, {identity}, "assignment")
    assert "uncovered_goals" in feedback and goal["id"] in feedback


async def test_deleted_source_coverage_does_not_complete_its_structural_check(source_service):
    from app.review.planning import plan_review

    plan = plan_review([{"file_path": "removed.py", "change_type": "deleted", "hunks": [{
        "old_start": 1, "old_length": 1, "new_start": 0, "new_length": 0, "lines": [],
    }]}], depth="standard")
    packet = await source_service.read("removed.py", 1, 1, "base")
    identity = packet["evidence"]["id"]
    goals = [goal.model_dump() for goal in plan.goals]
    output = {"examined": [{"goal_id": goal["id"], "evidence_ids": [identity]} for goal in goals]}
    result = coverage(source_service, goals, output, [identity])
    assert {goal["kind"]: goal["status"] for goal in result} == {
        "source": "examined", "structural": "not_examined"}

    commit_sources(source_service, {"other.py": "from removed import OLD\n"})
    caller = await source_service.read("other.py", 1, 1)
    caller_id = caller["evidence"]["id"]
    output["examined"][1]["evidence_ids"] = [caller_id]
    result = coverage(source_service, goals, output, [identity, caller_id])
    assert all(goal["status"] == "examined" for goal in result)


async def test_structural_goal_does_not_request_a_fabricated_source_range(source_service):
    goal = ReviewGoal(id="rename", kind="structural", path="safe.py", side="head",
                      description="检查旧路径的导入引用").model_dump()
    context = AgentContext(evidence=source_service, changed_files=[{
        "file_path": "safe.py", "old_file_path": "other.py",
        "change_type": "renamed", "hunks": []}])
    materials, issued = await prepare_materials(context, {"goals": [goal]})
    assert materials["source_materials"] == [] and materials["files"] == []
    assert issued == set()
    assert materials["change_materials"]["files"][0]["old_file_path"] == "other.py"


async def test_range_gaps_and_stale_coverage_cannot_be_hidden(source_service):
    commit_sources(source_service, {"safe.py": "line\n" * 6})
    reads = [await source_service.read("safe.py", first, last) for first, last in [(1, 2), (4, 6)]]
    issued = [item["evidence"]["id"] for item in reads]
    goal = ReviewGoal(id="head", path="safe.py", side="head", start_line=1, end_line=6,
                      status="examined").model_dump()
    result = coverage(source_service, [goal], {
        "examined": [{"goal_id": "head", "evidence_ids": issued}],
    }, issued)
    assert result[0]["status"] == "not_examined"
    assert "3–3" in result[0]["reason"]
    assert uncovered_ranges(1, 6, [(1, 2), (4, 6)]) == [(3, 3)]


async def test_preload_reserves_core_ranges_for_every_goal_before_expanding(source_service):
    texts = {"safe.py": "A" * 50 + "\n", "other.py": "B" * 60 + "\n"}
    commit_sources(source_service, {path: text * (90 if path == "safe.py" else 12)
                                    for path, text in texts.items()})
    source_service.snapshot = source_service.snapshot.model_copy(
        update={"base_sha": source_service.snapshot.head_sha})
    expected = [ReviewGoal(id=f"{path}-{side}", path=path, side=side,
        start_line=50 if path == "safe.py" else 8,
        end_line=52 if path == "safe.py" else 10).model_dump()
        for path in ("safe.py", "other.py") for side in ("head", "base")]
    context = AgentContext(evidence=source_service,
        changed_files=[{"file_path": path, "change_type": "modified", "hunks": []} for path in texts],
        symbols=[{"file": "safe.py", "start_line": 10, "end_line": 83}])
    materials, issued = await prepare_materials(context, {"goals": expected})
    packets = materials["source_materials"]
    assert len(packets) <= 6
    assert sum(len(packet["content"]) for packet in packets) <= 4000
    for goal in expected:
        refs = [packet["evidence"] for packet in packets
                if packet["evidence"]["path"] == goal["path"] and packet["evidence"]["side"] == goal["side"]]
        assert not uncovered_ranges(goal["start_line"], goal["end_line"],
                                    [(ref["start_line"], ref["end_line"]) for ref in refs])
    for index, packet in enumerate(packets):
        ref = packet["evidence"]
        source_service.resolve(ref["id"])
        for previous in packets[:index]:
            other = previous["evidence"]
            assert (ref["path"], ref["side"]) != (other["path"], other["side"]) or (
                ref["end_line"] < other["start_line"] or ref["start_line"] > other["end_line"])
    assert issued == {packet["evidence"]["id"] for packet in packets}


async def test_oversized_core_does_not_starve_smaller_goals_or_gain_coverage(source_service):
    commit_sources(source_service, {"safe.py": "A" * 5000 + "\n", "other.py": "small\n"})
    expected = [ReviewGoal(id=path, path=path, side="head", start_line=1, end_line=1).model_dump()
                for path in ("safe.py", "other.py")]
    context = AgentContext(evidence=source_service,
        changed_files=[{"file_path": goal["path"], "hunks": []} for goal in expected])
    materials, issued = await prepare_materials(context, {"goals": expected})
    assert [packet["evidence"]["path"] for packet in materials["source_materials"]] == ["other.py"]
    assert any(item["path"] == "safe.py" for item in materials["context_omitted"])
    result = coverage(source_service, expected, {"examined": []}, issued)
    assert all(item["status"] == "not_examined" for item in result)


@pytest.mark.parametrize("start,end", [(120, 390), (120, 360)])
async def test_inclusive_range_error_provides_a_bounded_correction(source_service, start, end):
    result = await read_file.coroutine(path="safe.py", start=start, end=end,
                                      runtime=tool_runtime(source_service))
    assert result["error"] == "source_range_too_large"
    assert result["requested_lines"] == end - start + 1
    first, last = result["suggested_range"]
    assert (first, last) == (start, start + 239)
    assert last - first + 1 == result["max_lines"] == 240
    assert result["next_start"] == last + 1
    assert "evidence" not in result and "available_range" not in result


class SubmissionModel(ToolCallingModel):
    calls: int = 0
    mistake: str = "side"
    repeat: bool = False
    preserve_gap: bool = False
    feedback_seen: bool = False

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        payload = json.loads(next(message.content for message in messages if message.type == "human"))
        self.feedback_seen = self.feedback_seen or any(
            isinstance(message, ToolMessage) and "review_submission_needs_correction" in message.content
            for message in messages)
        refs = {packet["evidence"]["side"]: packet["evidence"]["id"]
                for packet in payload.get("source_materials", [])}
        wrong = self.calls == 1 or self.repeat
        examined = [{"goal_id": goal["id"], "evidence_ids": [
            refs["head"] if wrong and self.mistake == "side" else
            "0" * 64 if wrong and self.mistake == "unissued" else refs[goal["side"]]]}
            for goal in payload.get("goals", [])]
        if wrong and self.mistake == "missing":
            examined = []
        if wrong and self.mistake == "unknown":
            examined.append({"goal_id": "unknown", "evidence_ids": [refs["head"]]})
        limitations, boundaries = [], []
        if self.mistake == "boundary":
            note = "未执行代码：数值效果由源码推断"
            if wrong or self.preserve_gap:
                limitations = [{
                    "goal_ids": [goal["id"] for goal in payload["goals"]],
                    "missing_fact": note + "；必要调用方仍未读取" if self.preserve_gap else note,
                    "dependent_conclusion": "返回值是否符合调用方契约",
                    "change_relevance": "本次返回值变化", "next_check": "读取必要调用方",
                }]
            else:
                boundaries = [note]
        args = {"findings": [], "examined": examined, "limitations": limitations, "boundary_notes": boundaries}
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[
            {"name": "DiscoverySubmission", "args": args, "id": f"submission-{self.calls}"}],
            usage_metadata={"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}))])


@pytest.mark.parametrize("mistake", ["side", "unissued", "missing", "unknown"])
async def test_native_submission_correction_reuses_issued_evidence_inside_budget(source_service, mistake):
    model = SubmissionModel(mistake=mistake)
    context = review_context(source_service, model)
    context.limits = ReviewLimits(model_calls=3, total_model_calls=8)
    result = await run_agent(context, "assignment", "reviewer", {"goals": goals()},
                             discovery_agent_ids={"assignment"})
    assert result["status"] == "completed" and model.feedback_seen
    assert result["model_calls"] == model.calls == 2
    assert result["structured_output_retries"] == 1
    assert result["tool_failures"] == 0 and result["input_tokens"] == 40
    checked = coverage(source_service, goals(), result["output"], result["evidence_ids"])
    assert all(goal["status"] == "examined" for goal in checked)
    assert (await context.budget.summary())["model_calls"] == 2


@pytest.mark.parametrize("limit,expected_calls", [(1, 1), (4, 2)])
async def test_unfixed_submission_stays_partial_and_feedback_does_not_loop(source_service, limit, expected_calls):
    model = SubmissionModel(repeat=True)
    context = review_context(source_service, model)
    context.limits = ReviewLimits(model_calls=limit)
    result = await run_agent(context, "assignment", "reviewer", {"goals": goals()})
    assert result["status"] == "completed"
    assert result["model_calls"] == expected_calls
    checked = coverage(source_service, goals(), result["output"], result["evidence_ids"])
    assert checked[1]["status"] == "not_examined"
    assert "侧别不匹配" in checked[1]["reason"]


class ReadingSubmissionModel(ToolCallingModel):
    calls: int = 0
    visible: list[str] = []

    def bind_tools(self, tools, **kwargs):
        self.visible = [tool.name if hasattr(tool, "name") else tool["function"]["name"]
                        for tool in tools]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        payload = json.loads(next(message.content for message in messages if message.type == "human"))
        reads = [json.loads(message.content) for message in messages
                 if isinstance(message, ToolMessage) and message.name in {"read_file", "read_base_file"}]
        feedback = any(isinstance(message, ToolMessage)
                       and "review_submission_needs_correction" in message.content for message in messages)
        if not reads or (len(reads) == 1 and feedback and "read_base_file" in self.visible):
            calls = [{"name": "read_base_file" if reads else "read_file",
                      "args": {"path": "safe.py", "start": 1, "end": 1}, "id": f"read-{self.calls}"}]
        else:
            refs = {item["evidence"]["side"]: item["evidence"]["id"] for item in reads}
            examined = [{"goal_id": goal["id"], "evidence_ids": [
                refs.get(goal["side"], refs["head"])]} for goal in payload["goals"]
                if not feedback or goal["side"] in refs]
            calls = [{"name": "DiscoverySubmission", "id": f"submission-{self.calls}", "args": {
                "findings": [], "examined": examined,
                "limitations": [{
                    "goal_ids": [goal["id"] for goal in payload["goals"] if goal["side"] == "base"],
                    "missing_fact": "缺少 BASE 源码，对照尚未完成",
                    "dependent_conclusion": "前后行为是否变化", "change_relevance": "修改返回值",
                    "next_check": "读取 BASE safe.py",
                }] if feedback and "base" not in refs else [],
            }}]
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=calls,
            usage_metadata={"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}))])


@pytest.mark.parametrize("limit,calls,complete", [(5, 4, True), (3, 3, False)])
async def test_feedback_can_read_missing_base_only_while_exploration_is_allowed(
        source_service, limit, calls, complete):
    model = ReadingSubmissionModel()
    context = review_context(source_service, model, depth="quick")
    context.limits = ReviewLimits(model_calls=limit, total_model_calls=10)
    result = await run_agent(context, "assignment", "reviewer", {"goals": goals()},
                             discovery_agent_ids={"assignment"})
    assert result["status"] == "completed"
    assert result["model_calls"] == calls <= limit
    assert result["structured_output_retries"] == 1
    checked = coverage(source_service, goals(), result["output"], result["evidence_ids"])
    assert all(goal["status"] == "examined" for goal in checked) is complete
    assert bool(result["investigation_limitations"]) is not complete


@pytest.mark.parametrize("preserve_gap", [False, True])
async def test_boundary_note_misclassification_does_not_consume_correction(source_service, preserve_gap):
    model = SubmissionModel(mistake="boundary", preserve_gap=preserve_gap)
    context = review_context(source_service, model)
    result = await run_agent(context, "assignment", "reviewer", {"goals": goals()})
    assert result["status"] == "completed" and model.calls == 1 and not model.feedback_seen
    assert result["structured_output_retries"] == 0
    assert result["investigation_limitations"]
    assert "未执行代码" in result["investigation_limitations"][0]
    assert ("必要调用方" in result["investigation_limitations"][0]) is preserve_gap


async def test_honest_partial_submission_does_not_receive_pointless_coverage_feedback(source_service):
    output = DiscoveryOutput(limitations=["必要调用方无法读取"])
    assert submission_feedback(output, goals(), source_service, set(), "assignment") is None


async def test_feedback_marker_and_call_count_survive_sqlite_reopen(source_service, tmp_path):
    head = await source_service.read("safe.py", 1, 1)
    base = await source_service.read("safe.py", 1, 1, "base")
    payload = {"goals": goals(), "source_materials": [head, base]}
    context = AgentContext(evidence=source_service, goals=tuple(goals()), budget_agent_id="assignment",
        material_evidence_ids=frozenset({head["evidence"]["id"], base["evidence"]["id"]}))
    model, limits = SubmissionModel(repeat=True), ReviewLimits(model_calls=4)
    config = {"configurable": {"thread_id": "feedback"}}
    database = str(tmp_path / "feedback.sqlite")

    def build(saver, interrupt):
        from app.review.models import DiscoverySubmission

        return create_agent(model, tools=REVIEW_TOOLS, response_format=ToolStrategy(DiscoverySubmission),
            middleware=[ReviewGuardMiddleware(limits), ModelCallLimitMiddleware(
                run_limit=4, thread_limit=4, exit_behavior="error"), ToolCallLimitMiddleware(
                    run_limit=80, thread_limit=80, exit_behavior="error")],
            context_schema=AgentContext, checkpointer=saver,
            interrupt_after=["ReviewGuardMiddleware.after_model"] if interrupt else None)

    async with aiosqlite.connect(database) as connection:
        graph = build(AsyncSqliteSaver(connection, serde=_checkpoint_serializer()), True)
        await graph.ainvoke({"messages": [("user", json.dumps(payload))]}, context=context, config=config)
        state = await graph.aget_state(config)
        assert state.values["review_feedback_sent"]
        assert state.values["structured_response"] is None
        assert state.values["thread_model_call_count"] == 1
    async with aiosqlite.connect(database) as connection:
        graph = build(AsyncSqliteSaver(connection, serde=_checkpoint_serializer()), False)
        result = await graph.ainvoke(None, context=context, config=config)
        assert model.calls == 2
        assert result["review_feedback_sent"]
        state = await graph.aget_state(config)
        assert state.values["thread_model_call_count"] == 2
