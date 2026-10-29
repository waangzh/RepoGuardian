import json
import asyncio

import aiosqlite
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import NodeCancelledError

from app.review.checkpointer import _checkpoint_serializer
from app.review.budget import ReviewBudget
from app.review.graph import build_review_graph
from app.review.limitations import validate_assessments
from app.review.models import LimitationAssessment, LimitationReviewOutput, ReviewGoal, ReviewLimits, ReviewReport
from app.review.recovery import retry_updates
from app.services.review.review_outcome import derive_review_outcome
from tests.review.test_limitation_reconciliation import task_for
from tests.review.test_native_graph import ReviewModel, review_context

pytest_plugins = ["tests.review.test_native_agents"]

DECLARATION = "未穷尽所有构造路径，尚不能证明两个聚合结果在所有输入下始终一致"


class ScopeModel(ReviewModel):
    assessment_status: str = "boundary"
    scope_failure: bool = False
    scope_finding: bool = False
    verifier_failure: bool = False
    scope_calls: int = 0
    introduce_limitation: bool = True

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        payload = json.loads(next(message.content for message in messages if message.type == "human"))
        if messages[0].content.startswith("You independently review proposed evidence gaps"):
            self.scope_calls += 1
            self.observed.append({"scope_review": payload})
            if self.scope_failure:
                raise RuntimeError("fixture_scope_failure")
            evidence = [item["evidence"]["id"] for item in payload["source_materials"]]
            evidence += [json.loads(item.content)["evidence"]["id"] for item in messages
                         if isinstance(item, ToolMessage) and item.name in {"read_file", "read_base_file"}
                         and json.loads(item.content).get("ok")]
            if not evidence:
                calls = [{"name": "read_file" if goal["side"] == "head" else "read_base_file",
                          "args": {"path": goal["path"], "start": goal["start_line"], "end": goal["end_line"]},
                          "id": f"scope-read-{index}"}
                         for index, goal in enumerate(payload["goals"])]
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=calls,
                    usage_metadata={"input_tokens": 30, "output_tokens": 20, "total_tokens": 50}))])
            goal_ids = [item["id"] for item in payload["goals"]]
            assessments = [{
                "limitation_id": item["id"], "status": self.assessment_status,
                "explanation": "只检查固定变更的必要前提，不能把全局正确性证明当作完成条件",
                "goal_ids": goal_ids, "evidence_ids": evidence,
                **({"missing_fact": "必要调用方的输入校验条件",
                    "dependent_conclusion": "该变更是否会让调用方接受无效输入",
                    "change_relevance": "修改直接改变 safe.py 的返回值，调用方契约依赖该值",
                    "next_check": "读取该调用方的输入校验函数"}
                   if self.assessment_status == "unresolved" else {}),
            } for item in payload["review_limitations"]]
            findings = [{
                "title": "复核发现的行为回归", "trigger": "读取 VALUE", "expected_behavior": "返回 1",
                "actual_behavior": "返回 2", "impact": "调用方收到错误值", "recommendation": "恢复契约",
                "severity": "medium", "affected_path": "safe.py", "line": 1,
                "evidence_ids": evidence, "attribution": "introduced",
            }] if self.scope_finding else []
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="",
                tool_calls=[{"name": "LimitationReviewOutput", "id": f"scope-{self.scope_calls}",
                             "args": {"assessments": assessments, "findings": findings}}],
                usage_metadata={"input_tokens": 30, "output_tokens": 20, "total_tokens": 50}))])
        if self.verifier_failure and ("candidate" in payload or "candidates" in payload):
            raise RuntimeError("fixture_verification_failure")
        response = super()._generate(messages, stop, run_manager, **kwargs)
        for call in response.generations[0].message.tool_calls:
            if call["name"] == "DiscoveryOutput":
                call["args"]["findings"] = []
                call["args"]["limitations"] = [DECLARATION] if self.introduce_limitation else []
        return response


async def execute(service, model, checkpointer=None, config=None):
    state = await build_review_graph(checkpointer).ainvoke(
        {"review_id": "review"}, config, context=review_context(service, model))
    report = ReviewReport.model_validate(state["report"])
    return state, report, derive_review_outcome(task_for(report), evidence=service)


@pytest.mark.parametrize("status,kind,gaps", [
    ("boundary", "clean", 0), ("resolved", "clean", 0), ("unresolved", "inconclusive", 1),
])
async def test_zero_candidates_receive_independent_bounded_necessity_review(source_service, status, kind, gaps):
    model = ScopeModel(assessment_status=status)
    _, report, outcome = await execute(source_service, model)
    assert not report.candidates and not report.verification_results
    assert report.complete and report.termination == "completed"
    assert len(report.limitation_results) == model.scope_calls == 1
    assert outcome.kind == kind and outcome.incomplete_investigations == gaps
    assert outcome.pending_limitation_count == 0
    record = outcome.review_limitations[0]
    assert record.status == status and record.assessment.goal_ids
    assert record.goal_ids == sorted(goal.id for goal in report.goals)
    if gaps:
        assert record.assessment.missing_fact and record.assessment.next_check
        assert record.assessment.change_relevance and record.assessment.dependent_conclusion
    # Scope review reads are independent and never grant additional Goal coverage.
    assert {item.id for item in report.goals} == {item["id"] for item in model.observed[0]["goals"]}
    assert report.discovery_results[0].investigation_limitations == [DECLARATION]


async def test_no_declaration_requires_no_extra_model_call(source_service):
    model = ScopeModel(introduce_limitation=False)
    _, report, outcome = await execute(source_service, model)
    assert model.scope_calls == 0 and not report.limitation_results
    assert outcome.kind == "clean"


class MixedAssessmentModel(ScopeModel):
    repeat_invalid: bool = True

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop, run_manager, **kwargs)
        corrected = any(isinstance(message, ToolMessage)
                        and "invalid_limitation_assessments" in str(message.content) for message in messages)
        for call in result.generations[0].message.tool_calls:
            if call["name"] == "DiscoveryOutput":
                call["args"]["limitations"] = ["配置读取前提 A 未确认", "配置读取前提 B 未确认"]
            if call["name"] == "LimitationReviewOutput" and (self.repeat_invalid or not corrected):
                call["args"]["assessments"][1]["evidence_ids"] = ["pagination-is-not-source"]
        return result


class RangeScopeModel(ScopeModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if messages[0].content.startswith("You independently review proposed evidence gaps"):
            reads = [json.loads(message.content) for message in messages
                     if isinstance(message, ToolMessage) and message.name == "read_file"]
            bounds = [1, 47] if not reads else reads[-1].get("suggested_range")
            if bounds:
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[{
                    "name": "read_file", "args": {"path": "other.py", "start": bounds[0], "end": bounds[1]},
                    "id": f"bounded-read-{len(reads)}"}],
                    usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}))])
        return super()._generate(messages, stop, run_manager, **kwargs)


async def test_scope_corrects_out_of_bounds_read_inside_original_budget(source_service):
    _, report, outcome = await execute(source_service, RangeScopeModel())
    result = report.limitation_results[0]
    assert result.status == "completed" and result.model_calls == 3
    assert result.tool_failures == 1 and outcome.pending_limitation_count == 0
    assert result.model_calls <= 5


@pytest.mark.parametrize("repeat_invalid", [False, True])
async def test_framework_correction_and_partial_admission_keep_runtime_truth(source_service, repeat_invalid):
    model = MixedAssessmentModel(repeat_invalid=repeat_invalid)
    _, report, outcome = await execute(source_service, model)
    assert model.scope_calls == 2
    assert report.limitation_results[0].structured_output_retries == 1
    assert outcome.pending_limitation_count == int(repeat_invalid)
    if repeat_invalid:
        assert report.termination == "partial" and outcome.runtime_reliability == "unreliable"
        assert report.limitation_results[0].error == "limitation_assessment_evidence_not_read"
        assert sorted(item.status for item in outcome.review_limitations) == ["boundary", "pending"]
    else:
        assert report.termination == "completed" and outcome.kind == "clean"


async def test_partial_assessment_survives_sqlite_reopen_and_failed_retry(source_service, tmp_path):
    database = tmp_path / "partial-assessment.db"
    config = {"configurable": {"thread_id": "partial-assessment"}}
    context = review_context(source_service, MixedAssessmentModel())
    async with aiosqlite.connect(database) as connection:
        graph = build_review_graph(AsyncSqliteSaver(connection, serde=_checkpoint_serializer()))
        first = await graph.ainvoke({"review_id": "review"}, config, context=context)
    original = first["limitation_results"][0]
    update = retry_updates(first, context.limits)
    resumed = review_context(source_service, ScopeModel(scope_failure=True))
    resumed.budget = context.budget
    resumed.retry_results = {(item["reviewer"], item["result"]["id"]): item["result"]
                            for item in update["retry_results"]}
    async with aiosqlite.connect(database) as connection:
        graph = build_review_graph(AsyncSqliteSaver(connection, serde=_checkpoint_serializer()))
        await graph.aupdate_state(config, update, as_node="plan")
        state = await graph.ainvoke(None, config, context=resumed)
    report = ReviewReport.model_validate_json(ReviewReport.model_validate(state["report"]).model_dump_json())
    outcome = derive_review_outcome(task_for(report), evidence=source_service)
    result = report.limitation_results[0]
    assert result.id == original["id"]
    assert result.previous_attempts[0].output == original["output"]
    assert result.model_calls == original["model_calls"] + 1
    assert outcome.pending_limitation_count == 1 and report.termination == "partial"
    assert sorted(item.status for item in outcome.review_limitations) == ["boundary", "pending"]


async def test_scope_failure_preserves_pending_and_usage_without_claiming_a_necessary_gap(source_service):
    model = ScopeModel(scope_failure=True)
    _, report, outcome = await execute(source_service, model)
    assert report.termination == "partial" and report.limitation_results[0].status == "failed"
    assert outcome.kind == "inconclusive" and outcome.pending_limitation_count == 1
    assert outcome.incomplete_investigations == 0
    assert "limitation_assessment_incomplete" in outcome.reason_codes
    assert "investigation_incomplete" not in outcome.reason_codes
    assert report.limitation_results[0].input_tokens is None


async def test_scope_discovery_enters_normal_independent_verification_once(source_service):
    model = ScopeModel(scope_finding=True)
    state, report, outcome = await execute(source_service, model)
    assert len(report.candidates) == len(report.verification_results) == len(report.findings) == 1
    assert model.scope_calls == state["limitation_round"] == 1
    assert report.candidates[0].assignment_ids == [report.limitation_results[0].id]
    assert outcome.kind == "findings" and outcome.confirmed_count == 1
    assert not outcome.pending_limitation_count and not outcome.incomplete_investigations
    from app.services.publication.review_publication import validated_findings
    from app.models.review.diff import ChangedFile
    from tests.review.test_native_graph import CHANGE
    from types import SimpleNamespace
    from app.models.review_plan import ReviewPlanOptions
    task = task_for(report)
    task.configuration = SimpleNamespace(options=ReviewPlanOptions())
    task.changed_files = [ChangedFile.model_validate({**CHANGE, "additions": 1, "deletions": 1})]
    findings, withheld = validated_findings(task, source_service.artifacts)
    assert findings == report.findings and not withheld


@pytest.mark.parametrize("fault", ["unknown_goal", "other_file", "unread", "unknown_id", "duplicate", "omitted"])
async def test_assessment_members_scope_and_independent_source_are_enforced(source_service, fault):
    head = await source_service.read("safe.py", 1, 1)
    other = await source_service.read("other.py", 1, 1)
    identity = other["evidence"]["id"] if fault == "other_file" else head["evidence"]["id"]
    goal = {"id": "head", "path": "safe.py", "side": "head"}
    record = {"id": "gap", "goal_ids": ["head"]}
    assessment = LimitationAssessment(limitation_id="gap", status="boundary", explanation="固定范围之外的证明要求",
        goal_ids=["head"], evidence_ids=[identity])
    if fault == "unknown_goal":
        assessment.goal_ids = ["elsewhere"]
    if fault == "unknown_id":
        assessment.limitation_id = "invented"
    output = LimitationReviewOutput(assessments=[assessment] * (2 if fault == "duplicate" else 1))
    limitations = [record, {"id": "omitted", "goal_ids": ["head"]}] if fault == "omitted" else [record]
    with pytest.raises((ValueError, OSError)):
        validate_assessments(output, limitations, [goal], [] if fault == "unread" else [identity], source_service)


@pytest.mark.parametrize("status", ["resolved", "boundary"])
@pytest.mark.parametrize("with_source_goal", [False, True])
@pytest.mark.parametrize("goal_path,path,side", [
    ("safe.py", "safe.py", "head"),
    ("safe.py", "other.py", "base"),
    ("removed.py", "removed.py", "base"),
])
async def test_structural_assessment_cannot_close_with_target_or_base_evidence(
        source_service, status, with_source_goal, goal_path, path, side):
    from tests.review.test_retrieval import commit_sources

    commit_sources(source_service, {"other.py": f"import {goal_path.removesuffix('.py')}\n"})
    structural = ReviewGoal(id="structural", kind="structural", path=goal_path,
        side="base" if goal_path == "removed.py" else "head").model_dump()
    goals = [structural]
    packet = await source_service.read(path, 1, 1, side)
    identity = packet["evidence"]["id"]
    if with_source_goal:
        goals.append(ReviewGoal(id="source", path=path, side=side,
            start_line=1, end_line=1).model_dump())
    goal_ids = [goal["id"] for goal in goals]
    limitation = {"id": "gap", "goal_ids": goal_ids}
    assessment = LimitationAssessment(limitation_id="gap", status=status, explanation="检查引用方",
        goal_ids=goal_ids, evidence_ids=[identity])
    output = LimitationReviewOutput(assessments=[assessment])

    with pytest.raises(ValueError, match="limitation_assessment_structural_source_missing"):
        validate_assessments(output, [limitation], goals, [identity], source_service)

    caller = await source_service.read("other.py", 1, 1)
    caller_id = caller["evidence"]["id"]
    assessment.evidence_ids.append(caller_id)
    validate_assessments(output, [limitation], goals, [identity, caller_id], source_service)


def test_vague_unresolved_caveat_is_not_a_valid_necessary_gap():
    with pytest.raises(ValueError, match="necessary_gap_requires"):
        LimitationAssessment(limitation_id="gap", status="unresolved", explanation="没有检查所有路径",
            goal_ids=["head"], evidence_ids=["source"])


async def test_completed_scope_review_survives_sqlite_reopen_without_repeating_calls(source_service, tmp_path):
    config = {"configurable": {"thread_id": "scope-review"}}
    path = tmp_path / "scope-checkpoints.db"
    async with aiosqlite.connect(path) as connection:
        saver = AsyncSqliteSaver(connection, serde=_checkpoint_serializer())
        model = ScopeModel()
        _, report, _ = await execute(source_service, model, saver, config)
        calls = model.scope_calls
    async with aiosqlite.connect(path) as connection:
        saver = AsyncSqliteSaver(connection, serde=_checkpoint_serializer())
        model = ScopeModel(scope_failure=True)
        result = await build_review_graph(saver).ainvoke(None, config, context=review_context(source_service, model))
        assert ReviewReport.model_validate(result["report"]) == report
        assert model.scope_calls == 0 and calls == 1


async def test_retry_keeps_completed_discovery_and_cumulative_scope_budget(source_service):
    model = ScopeModel(scope_failure=True)
    state, report, _ = await execute(source_service, model)
    update = retry_updates(state, review_context(source_service, model).limits)
    assert update["retry_results"][0]["reviewer"] == "limitation_reviewer"
    assert update["retry_results"][0]["result"] == report.limitation_results[0].model_dump()
    assert update["limitation_round"] == 0


async def test_invalid_archived_assessment_returns_to_pending(source_service):
    _, report, _ = await execute(source_service, ScopeModel())
    identity = report.limitation_results[0].output["assessments"][0]["evidence_ids"][0]
    (source_service.artifacts.root / "review" / f"evidence-{identity}.txt").unlink()
    outcome = derive_review_outcome(task_for(report), evidence=source_service)
    assert outcome.kind == "inconclusive" and outcome.pending_limitation_count == 1
    assert outcome.incomplete_investigations == 0


async def test_global_budget_exhaustion_keeps_declaration_pending(source_service):
    model = ScopeModel()
    context = review_context(source_service, model)
    context.limits = ReviewLimits(total_model_calls=6)
    context.budget = ReviewBudget(context.limits)
    for index in range(3):
        ticket = await context.budget.reserve(f"earlier-{index}", 1)
        await context.budget.settle(ticket, {"input_tokens": 1, "output_tokens": 0})
    result = await build_review_graph().ainvoke({"review_id": "review"}, context=context)
    report = ReviewReport.model_validate(result["report"])
    outcome = derive_review_outcome(task_for(report), evidence=source_service)
    assert report.complete and report.termination == "partial"
    assert report.limitation_results[0].error == "ReviewBudgetExceeded"
    assert outcome.pending_limitation_count == 1 and not outcome.incomplete_investigations
    assert (await context.budget.summary())["model_calls"] <= 6
    assert model.scope_calls == 0


async def test_cancellation_before_scope_model_call_cannot_become_a_complete_report(source_service):
    model = ScopeModel()
    cancelled = False

    async def active():
        if cancelled:
            raise asyncio.CancelledError()

    async def progress(event):
        nonlocal cancelled
        if event.get("phase") == "limitation_reviewer" and event.get("status") == "running":
            cancelled = True

    context = review_context(source_service, model, check_active=active, progress=progress)
    with pytest.raises(NodeCancelledError):
        await build_review_graph().ainvoke({"review_id": "review"}, context=context)
    assert model.scope_calls == 0


@pytest.mark.parametrize("depth,limit", [("quick", 6), ("deep", 20)])
async def test_scope_review_respects_depth_and_quick_reads_on_demand(source_service, depth, limit):
    model = ScopeModel()
    context = review_context(source_service, model, depth=depth)
    context.limits = ReviewLimits(model_calls=limit)
    result = await build_review_graph().ainvoke({"review_id": "review"}, context=context)
    report = ReviewReport.model_validate(result["report"])
    outcome = derive_review_outcome(task_for(report), evidence=source_service)
    assert outcome.kind == "clean"
    assert model.scope_calls == (2 if depth == "quick" else 1)
    assert report.limitation_results[0].evidence_ids
    assert report.limitation_results[0].model_calls <= (3 if depth == "quick" else 7)


async def test_scope_candidate_cannot_bypass_independent_verification(source_service):
    _, report, outcome = await execute(source_service, ScopeModel(scope_finding=True, invalid_verifier=True))
    assert len(report.candidates) == 1 and not report.findings
    assert outcome.unresolved_count == 1 and outcome.kind == "inconclusive"


async def test_retry_of_scope_discovered_candidate_keeps_candidate_and_completed_scope_review(source_service):
    config = {"configurable": {"thread_id": "scope-candidate-retry"}}
    graph = build_review_graph(InMemorySaver())
    context = review_context(source_service, ScopeModel(scope_finding=True, verifier_failure=True))
    first = await graph.ainvoke({"review_id": "review"}, config, context=context)
    assert first["report"]["termination"] == "partial"
    assert first["verification_results"][0]["status"] == "failed"
    original_candidate = first["candidates"][0]["id"]
    update = retry_updates(first, context.limits)
    await graph.aupdate_state(config, update, as_node="plan")
    model = ScopeModel(scope_failure=True)
    resumed = review_context(source_service, model)
    resumed.budget = context.budget
    resumed.retry_results = {(item["reviewer"], item["result"]["id"]): item["result"]
                            for item in update["retry_results"]}
    result = await graph.ainvoke(None, config, context=resumed)
    report = ReviewReport.model_validate(result["report"])
    assert report.termination == "completed"
    assert report.candidates[0].id == report.findings[0].id == original_candidate
    assert report.limitation_results == ReviewReport.model_validate(first["report"]).limitation_results
    assert model.scope_calls == 0
