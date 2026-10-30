import asyncio
import json
from types import SimpleNamespace

import pytest
import aiosqlite
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import NodeCancelledError
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.review.checkpointer import _checkpoint_serializer
from app.review.context import AgentContext, ReviewContext
from app.review.findings import candidate_gate, collect_candidates, finding_attribution, verify_finding
from app.review.graph import build_review_graph
from app.review.models import CandidateFinding, ReviewLimits, ReviewReport
from app.services.review.review_outcome import derive_review_outcome
from tests.review.test_native_agents import ToolCallingModel

pytest_plugins = ["tests.review.test_native_agents"]


CHANGE = {
    "file_path": "safe.py", "change_type": "modified",
    "hunks": [{"old_start": 1, "old_length": 1, "new_start": 1, "new_length": 1,
               "lines": [{"kind": "deleted", "old_line_no": 1, "content": "VALUE = 1"},
                         {"kind": "added", "new_line_no": 1, "content": "VALUE = 2"}]}],
}


class ReviewModel(ToolCallingModel):
    observed: list[dict] = []
    invalid_verifier: bool = False
    investigate_uncertain: bool = False

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        payload = json.loads(next(message.content for message in messages if message.type == "human"))
        responses = [message for message in messages if isinstance(message, ToolMessage)]
        verifying = "candidate" in payload or "candidates" in payload
        if not responses:
            self.observed.append(payload)
            calls = [
                {"name": name, "args": {"path": "safe.py", "start": 1, "end": 1}, "id": name}
                for name in ("read_file", "read_base_file")
            ]
        else:
            bodies = [json.loads(message.content) for message in responses]
            if any(not body.get("ok") for body in bodies):
                self.observed.append({"tool_errors": bodies})
            evidence = [body["evidence"] for body in bodies]
            identities = [item["id"] for item in evidence]
            if verifying:
                args = {"decision": "confirmed", "explanation": "独立读取确认此输入触发行为差异",
                        "evidence_ids": ["invented"] if self.invalid_verifier else identities,
                        "attribution": "introduced"}
                schema = "VerificationOutput"
                if "candidates" in payload:
                    args = {"decisions": [{"candidate_id": item["id"], **args}
                                          for item in payload["candidates"]]}
                    schema = "BatchVerificationOutput"
            else:
                args = {"findings": [{
                    "title": "行为回归", "trigger": "读取 VALUE", "expected_behavior": "返回 1",
                    "actual_behavior": "返回 2", "impact": "调用者收到错误值",
                    "recommendation": "恢复契约", "severity": "medium",
                    "affected_path": "safe.py", "line": 1, "evidence_ids": identities,
                    "attribution": "introduced",
                }], "examined": [
                    {"goal_id": goal["id"], "evidence_ids": [
                        item["id"] for item in evidence if item["side"] == goal["side"]]}
                    for goal in payload["goals"]
                ]}
                schema = "DiscoverySubmission"
            calls = [{"name": schema, "args": args, "id": "output"}]
        return ChatResult(generations=[ChatGeneration(message=AIMessage(
            content="", tool_calls=calls, usage_metadata={
                "input_tokens": 20, "output_tokens": 10, "total_tokens": 30}))])


def review_context(service, model, **kwargs):
    async def load():
        return AgentContext(evidence=service, changed_files=[CHANGE])
    return ReviewContext(load=load, model=model, limits=ReviewLimits(), **kwargs)


@pytest.mark.parametrize("_attempt", range(4))
async def test_native_graph_discovery_union_independent_verification_and_coverage(source_service, _attempt):
    model = ReviewModel()
    graph = build_review_graph(InMemorySaver())
    result = await graph.ainvoke({"review_id": "review"}, {"configurable": {"thread_id": "review"}},
                                context=review_context(source_service, model))
    report = ReviewReport.model_validate(result["report"])
    assert report.complete, json.dumps({
        "goals": [item.model_dump() for item in report.goals],
        "results": [item.model_dump() for item in report.discovery_results],
        "observed": model.observed}, ensure_ascii=False)
    assert report.termination == "completed"
    assert len(report.review_units) == 1 and len(report.assignments) == 1
    assert report.assignments[0].reviewer == "reviewer"
    assert report.assignments[0].unit_id == report.review_units[0].id
    assert len(report.goals) == 2  # Two sides, independent of reviewer count.
    assert len(report.candidates) == len(report.findings) == 1
    assert len(report.candidates[0].assignment_ids) == 1
    assert report.findings[0].inline_line == 1
    assert len(model.observed) == 2
    assert "assignment" in model.observed[0] and "candidate" not in model.observed[0]
    assert "candidate" in model.observed[-1] and "assignment" not in model.observed[-1]
    assert "messages" not in result


async def test_invalid_verification_does_not_become_confirmed_or_clean(source_service):
    result = await build_review_graph().ainvoke(
        {"review_id": "review"},
        context=review_context(source_service, ReviewModel(invalid_verifier=True)))
    report = ReviewReport.model_validate(result["report"])
    assert report.findings == []
    assert report.complete  # Code coverage is independent of verification.
    assert report.termination == "completed"
    assert report.decisions[0].decision == "insufficient_evidence"


@pytest.mark.parametrize("depth,model_calls", [("quick", 6), ("standard", 12), ("deep", 20)])
async def test_discovery_cannot_spend_the_verification_reserve(source_service, depth, model_calls):
    from app.review.configuration import discovery_limits

    class InvestigatingModel(ReviewModel):
        visible: list[str] = []

        def bind_tools(self, tools, **kwargs):
            self.visible = [tool.name if hasattr(tool, "name") else tool["function"]["name"]
                            for tool in tools]
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            payload = json.loads(next(message.content for message in messages if message.type == "human"))
            if "assignment" in payload and "read_file" in self.visible:
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content="",
                    tool_calls=[{"name": name, "args": {"path": "safe.py", "start": 1, "end": 1},
                                 "id": f"{name}-{len(messages)}"}
                                for name in ("read_file", "read_base_file")],
                    usage_metadata={"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}))])
            result = super()._generate(messages, stop, run_manager, **kwargs)
            for call in result.generations[0].message.tool_calls:
                if call["name"] == "DiscoverySubmission":
                    call["args"]["findings"][0]["severity"] = "high"
            return result

    context = review_context(source_service, InvestigatingModel(), depth=depth)
    context.limits = ReviewLimits(model_calls=model_calls, total_model_calls=8)
    result = await build_review_graph().ainvoke({"review_id": "review"}, context=context)
    report = ReviewReport.model_validate(result["report"])
    assert report.complete and report.termination == "completed"
    assert not report.coverage_limitations
    assert report.discovery_results[0].model_calls <= discovery_limits(context.limits, depth).total_model_calls
    assert all(item.status == "completed" for item in report.verification_results)
    assert report.decisions[0].decision == "confirmed"
    assert report.budget["model_calls"] <= 8


async def test_completed_send_branches_are_recovered_without_replaying_discovery(source_service):
    model, saver = ReviewModel(), InMemorySaver()
    fail_once = True

    async def progress(event):
        nonlocal fail_once
        if event["phase"] == "collect" and fail_once:
            fail_once = False
            raise RuntimeError("injected_process_failure")

    config = {"configurable": {"thread_id": "review"}}
    graph = build_review_graph(saver)
    with pytest.raises(RuntimeError, match="injected"):
        await graph.ainvoke({"review_id": "review"}, config,
                            context=review_context(source_service, model, progress=progress))
    assert len(model.observed) == 1
    recovered = await graph.ainvoke(None, config, context=review_context(source_service, model))
    assert len(model.observed) == 2  # Only the verifier started on resume.
    assert len(recovered["report"]["findings"]) == 1
    assert not (await graph.aget_state(config)).next


async def test_cancellation_stops_new_agent_calls(source_service):
    model = ReviewModel()

    async def inactive():
        raise asyncio.CancelledError()

    with pytest.raises(NodeCancelledError):
        await build_review_graph().ainvoke(
            {"review_id": "review"}, context=review_context(source_service, model, check_active=inactive))
    assert model.observed == []


async def test_concurrent_evidence_read_and_replay(source_service):
    results = await asyncio.gather(*(source_service.read("safe.py", 1, 1) for _ in range(24)))
    assert len({item["evidence"]["id"] for item in results}) == 1
    identity = results[0]["evidence"]["id"]
    assert source_service.resolve(identity).path == "safe.py"


def test_candidate_union_does_not_merge_different_triggers():
    proposal = {
        "title": "问题", "trigger": "输入 A", "expected_behavior": "正常", "actual_behavior": "失败",
        "impact": "丢失", "recommendation": "检查输入", "severity": "high", "affected_path": "safe.py",
        "line": 1, "evidence_ids": ["source"], "attribution": "introduced",
    }
    results = [{"id": "reviewer", "status": "completed", "output": {"findings": [proposal]}},
               {"id": "contract", "status": "completed",
                "output": {"findings": [{**proposal, "trigger": "输入 B"}]}}]
    assert len(collect_candidates(results)) == 2


async def test_verifier_cannot_confirm_head_bug_using_only_base(source_service):
    base = await source_service.read("safe.py", 1, 1, "base")
    identity = base["evidence"]["id"]
    candidate = CandidateFinding(
        id="candidate", assignment_ids=["reviewer"], title="问题", trigger="输入",
        expected_behavior="正常", actual_behavior="失败", impact="丢失", recommendation="修复",
        severity="high", affected_path="safe.py", line=1, evidence_ids=[identity],
        attribution="introduced")
    result = {"status": "completed", "evidence_ids": [identity], "output": {
        "decision": "confirmed", "explanation": "模型声称已确认", "evidence_ids": [identity],
        "attribution": "introduced"}}
    with pytest.raises(ValueError, match="head_failure_source_missing"):
        verify_finding(candidate, result, source_service, [CHANGE])
    with pytest.raises(ValueError, match="not_read_by_agent"):
        candidate_gate(candidate, source_service,
                       [{"id": "other-agent", "evidence_ids": [identity]}], [CHANGE])


async def test_verifier_corrects_discovery_attribution_with_evidence(source_service):
    head = await source_service.read("safe.py", 1, 1)
    base = await source_service.read("safe.py", 1, 1, "base")
    identities = [head["evidence"]["id"], base["evidence"]["id"]]
    candidate = CandidateFinding(id="candidate", assignment_ids=["reviewer"], title="问题",
        trigger="输入", expected_behavior="正常", actual_behavior="失败", impact="丢失",
        recommendation="修复", severity="high", affected_path="safe.py", line=1,
        evidence_ids=identities, attribution="introduced")
    output = {"status": "completed", "evidence_ids": identities, "output": {
        "decision": "confirmed", "explanation": "BASE 已存在缺陷，本次 PR 扩大了影响",
        "evidence_ids": identities, "attribution": "exposed", "severity": "high"}}
    decision, finding = verify_finding(candidate, output, source_service, [CHANGE])
    assert decision.attribution == finding_attribution(finding) == "exposed"
    assert finding.candidate.attribution == "introduced"  # 候选初始假设保持原样

    output["output"]["attribution"] = "uncertain"
    with pytest.raises(ValueError, match="finding_attribution_unverified"):
        verify_finding(candidate, output, source_service, [CHANGE])


async def test_database_cancel_is_checked_between_native_model_calls(source_service):
    model = ReviewModel()
    cancelled = False

    async def active():
        if cancelled:
            raise asyncio.CancelledError()

    async def progress(event):
        nonlocal cancelled
        if event.get("status") == "running":
            cancelled = True

    with pytest.raises(NodeCancelledError):
        await build_review_graph().ainvoke(
            {"review_id": "review"},
            context=review_context(source_service, model, check_active=active, progress=progress))
    assert model.observed == []


async def test_sqlite_reopen_restores_completed_native_branches(source_service, tmp_path):
    model = ReviewModel()
    config = {"configurable": {"thread_id": "review"}}
    database = str(tmp_path / "graph.sqlite")

    async def crash(event):
        if event["phase"] == "collect":
            raise RuntimeError("injected_shutdown")

    async with aiosqlite.connect(database) as connection:
        saver = AsyncSqliteSaver(connection, serde=_checkpoint_serializer())
        await saver.setup()
        with pytest.raises(RuntimeError, match="shutdown"):
            await build_review_graph(saver).ainvoke(
                {"review_id": "review"}, config,
                context=review_context(source_service, model, progress=crash))
    assert len(model.observed) == 1
    async with aiosqlite.connect(database) as connection:
        saver = AsyncSqliteSaver(connection, serde=_checkpoint_serializer())
        result = await build_review_graph(saver).ainvoke(
            None, config, context=review_context(source_service, model))
    assert len(model.observed) == 2
    assert result["report"]["complete"]
    assert len(result["report"]["findings"]) == 1


async def test_empty_scope_terminates_without_agents_or_false_clean(source_service):
    model = ReviewModel()
    async def load():
        return AgentContext(evidence=source_service, changed_files=[])
    result = await build_review_graph().ainvoke({"review_id": "review"},
        context=ReviewContext(load=load, model=model))
    assert not result["report"]["complete"]
    assert result["report"]["termination"] == "completed"
    assert model.observed == []


async def test_verifier_preserves_insufficient_without_fabricating_source(source_service):
    candidate = CandidateFinding(id="candidate", assignment_ids=["discover"], title="待核实",
        trigger="输入", expected_behavior="正常", actual_behavior="错误", impact="未知",
        recommendation="补查", severity="high", affected_path="safe.py", line=1,
        evidence_ids=["unknown"], attribution="uncertain")
    output = {"status": "completed", "output": {"decision": "insufficient_evidence", "explanation": "关键前提尚未找到",
        "evidence_ids": [], "counterevidence_ids": [], "attribution": "uncertain"}, "evidence_ids": []}
    decision, finding = verify_finding(candidate, output, source_service, [CHANGE])
    assert decision.decision == "insufficient_evidence" and finding is None
    output["output"]["decision"] = "refuted"
    with pytest.raises(ValueError, match="refutation_source_missing"):
        verify_finding(candidate, output, source_service, [CHANGE])


def test_verification_limitations_cannot_be_returned_as_confirmed_or_refuted():
    from app.review.models import VerificationOutput

    for decision in ("confirmed", "refuted"):
        with pytest.raises(ValueError, match="limitations_require_insufficient_evidence"):
            VerificationOutput(decision=decision, explanation="仍有关键来源未读取",
                attribution="uncertain", limitations=["关键调用方范围无法读取"])


def test_investigation_routes_only_once_for_high_impact_uncertainty():
    from app.review.graph import route_investigation
    state = {"review_id": "review", "decisions": [{"candidate_id": "a", "decision": "insufficient_evidence"}],
             "eligible_candidates": [{"id": "a"}], "candidates": [{"id": "a", "severity": "high"}]}
    assert len(route_investigation(state)) == 1
    assert route_investigation({**state, "investigation_round": 1}) == "report"


def test_investigation_depth_follows_the_review_strategy():
    from app.review.graph import route_investigation
    state = {"review_id": "review", "decisions": [{"candidate_id": "a", "decision": "insufficient_evidence"}],
             "eligible_candidates": [{"id": "a"}], "candidates": [{"id": "a", "severity": "medium"}]}
    assert route_investigation({**state, "review_depth": "quick"}) == "report"
    assert route_investigation({**state, "review_depth": "standard"}) == "report"
    assert len(route_investigation({**state, "review_depth": "deep"})) == 1
    high = {**state, "candidates": [{"id": "a", "severity": "high"}]}
    assert len(route_investigation({**high, "review_depth": "standard"})) == 1


def test_native_structured_outputs_survive_safe_checkpoint_serialization():
    from app.review.models import DiscoveryOutput, VerificationOutput
    serializer = _checkpoint_serializer()
    outputs = [DiscoveryOutput(), VerificationOutput(decision="insufficient_evidence",
               explanation="未取得必要前提", attribution="uncertain")]
    for output in outputs:
        restored = serializer.loads_typed(serializer.dumps_typed(output))
        assert restored == output
    assert not serializer.pickle_fallback


async def test_replayed_agent_work_does_not_reset_unknown_prior_usage_to_zero(source_service):
    context = review_context(source_service, ReviewModel(), replaying_nodes=frozenset({"discover"}))
    result = await build_review_graph().ainvoke({"review_id": "review"}, context=context)
    assert result["report"]["discovery_results"]
    assert all(item["model_calls"] is None and item["input_tokens"] is None
               for item in result["report"]["discovery_results"])


async def test_tool_failures_are_diagnostic_when_goal_coverage_is_complete(source_service):
    from langgraph.runtime import Runtime
    from app.review.nodes.reporting import report
    context = review_context(source_service, ReviewModel())
    state = await build_review_graph().ainvoke({"review_id": "review"}, context=context)
    state["discovery_results"][0]["tool_failures"] = 1
    result = await report(state, Runtime(context=context))
    assert result["report"]["complete"]
    assert result["report"]["termination"] == "completed"


class InvestigatedReviewModel(ReviewModel):
    """Verifier 报告证据不足，定向调查随后排除该疑点。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop, run_manager, **kwargs)
        investigator = any("bounded independent investigation" in str(message.content)
                           for message in messages if message.type == "system")
        for call in result.generations[0].message.tool_calls:
            if call["name"] == "DiscoverySubmission":
                call["args"]["findings"][0]["severity"] = "high"
            elif call["name"] == "VerificationOutput" and investigator:
                call["args"] = {"decision": "refuted", "explanation": "调用方已校验输入，疑点不成立",
                                "evidence_ids": call["args"]["evidence_ids"],
                                "attribution": "introduced"}
            elif call["name"] == "VerificationOutput":
                call["args"] = {"decision": "insufficient_evidence", "explanation": "缺少调用方证据",
                                "attribution": "uncertain", "limitations": ["未读取调用方"]}
        return result


async def test_resolved_investigation_is_not_reported_as_incomplete(source_service):
    model = InvestigatedReviewModel()
    state = await build_review_graph().ainvoke({"review_id": "review"},
        context=review_context(source_service, model))
    assert len(model.observed) == 3  # 一次主审发现、一次初始核验、一次定向调查
    saved_report = ReviewReport.model_validate(state["report"])
    assert len(saved_report.verification_results) == 2  # 历史执行记录完整保留
    assert any(item.investigation_limitations for item in saved_report.verification_results)
    assert saved_report.decisions[0].decision == "refuted" and not saved_report.findings
    task = SimpleNamespace(
        id="review", status="completed", error=None, review_report=saved_report,
        snapshot=SimpleNamespace(head_sha=saved_report.snapshot.head_sha,
                                 effective_base_sha=saved_report.snapshot.base_sha),
        pr=SimpleNamespace(owner="local", repo="repo", number=1),
    )
    outcome = derive_review_outcome(task)
    assert outcome.unresolved_count == 0
    assert outcome.incomplete_investigations == 0
    assert "investigation_incomplete" not in {item.value for item in outcome.reason_codes}
    assert outcome.kind == "clean"


async def test_usage_unknown_stays_telemetry_and_does_not_change_review_kind(source_service):
    state = await build_review_graph().ainvoke({"review_id": "review"},
        context=review_context(source_service, ReviewModel()))
    saved_report = ReviewReport.model_validate(state["report"])
    for result in saved_report.discovery_results + saved_report.verification_results:
        result.input_tokens = result.output_tokens = None
    task = SimpleNamespace(
        id="review", status="completed", error=None, review_report=saved_report,
        snapshot=SimpleNamespace(head_sha=saved_report.snapshot.head_sha,
                                 effective_base_sha=saved_report.snapshot.base_sha),
        pr=SimpleNamespace(owner="local", repo="repo", number=1),
    )
    outcome = derive_review_outcome(task)
    assert outcome.runtime_reliability == "unknown"
    assert "usage_integrity_unknown" in {item.value for item in outcome.reason_codes}
    # 计量未知只作遥测，不改变代码审查结论。
    assert outcome.kind == "findings"
    assert {item.value for item in outcome.reason_codes} == {"usage_integrity_unknown"}


async def test_explicit_investigation_limitations_do_not_masquerade_as_execution_failure(source_service):
    from langgraph.runtime import Runtime
    from app.review.nodes.reporting import report
    context = review_context(source_service, ReviewModel())
    state = await build_review_graph().ainvoke({"review_id": "review"}, context=context)
    state["discovery_results"][0]["investigation_limitations"] = ["调用方搜索仍有未读取范围"]
    result = await report(state, Runtime(context=context))
    assert result["report"]["complete"]
    assert result["report"]["termination"] == "completed"
    saved_report = ReviewReport.model_validate(result["report"])
    task = SimpleNamespace(
        id="review", status="completed", error=None, review_report=saved_report,
        snapshot=SimpleNamespace(head_sha=saved_report.snapshot.head_sha,
                                 effective_base_sha=saved_report.snapshot.base_sha),
        pr=SimpleNamespace(owner="local", repo="repo", number=1),
    )
    outcome = derive_review_outcome(task)
    assert outcome.kind == "findings"
    assert outcome.incomplete_investigations == 0 and outcome.pending_limitation_count == 0
    assert "limitation_assessment_incomplete" not in {item.value for item in outcome.reason_codes}
    assert outcome.review_limitations[0].text == "调用方搜索仍有未读取范围"
