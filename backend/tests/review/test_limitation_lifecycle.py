import asyncio
import json
from types import SimpleNamespace

import aiosqlite
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from app.review.agents import build_agent
from app.review.budget import ReviewBudget
from app.review.checkpointer import _checkpoint_serializer
from app.review.graph import build_review_graph
from app.review.models import DiscoveryGap, DiscoveryOutput, DiscoverySubmission, ReviewLimits, ReviewReport
from app.review.recovery import advance_retired_limitation_checkpoint, retry_updates
from app.review.state import ReviewState
from app.review.submission import submission_feedback
from app.services.review.review_outcome import derive_review_outcome
from app.services.review.review_repository import ReviewLeaseLost
from tests.review.test_limitation_reconciliation import gap, task_for
from tests.review.test_native_graph import ReviewModel, review_context

pytest_plugins = ["tests.review.test_native_agents"]

DECLARATION = "必要调用方的输入约束尚未确认"


class GapModel(ReviewModel):
    requests: int = 0
    emit_gap: bool = True

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.requests += 1
        assert not messages[0].content.startswith("You independently review proposed evidence gaps")
        payload = json.loads(next(message.content for message in messages if message.type == "human"))
        result = super()._generate(messages, stop, run_manager, **kwargs)
        for call in result.generations[0].message.tool_calls:
            if call["name"] == "DiscoverySubmission":
                call["args"]["findings"] = []
                call["args"]["limitations"] = (
                    [gap(DECLARATION, [goal["id"] for goal in payload["goals"]])] if self.emit_gap else [])
        return result


async def execute(service, model=None):
    model = model or GapModel()
    state = await build_review_graph().ainvoke(
        {"review_id": "review"}, context=review_context(service, model))
    report = ReviewReport.model_validate(state["report"])
    return state, report, derive_review_outcome(task_for(report), evidence=service)


async def test_local_gap_preserves_scope_and_no_findings_without_extra_model_phase(source_service):
    model = GapModel()
    _, report, outcome = await execute(source_service, model)
    assert report.policy_version == 2 and report.complete and report.termination == "completed"
    assert not report.candidates and not report.verification_results and not report.limitation_results
    assert model.requests == report.discovery_results[0].model_calls == 2
    assert outcome.kind == "clean" and outcome.runtime_reliability == "reliable"
    assert not outcome.reason_codes and outcome.pending_limitation_count == 0
    record = outcome.review_limitations[0]
    assert record.status == "reported" and record.text == DECLARATION
    assert record.gap.dependent_conclusion and record.gap.next_check
    assert record.goal_ids == sorted(goal.id for goal in report.goals)


@pytest.mark.parametrize("field", ["goal_ids", "missing_fact", "dependent_conclusion",
                                  "change_relevance", "next_check"])
def test_current_submission_requires_concrete_gap_fields(field):
    declaration = gap(DECLARATION, ["head"])
    declaration.pop(field)
    with pytest.raises(ValueError):
        DiscoverySubmission(limitations=[declaration])
    with pytest.raises(ValueError):
        DiscoverySubmission(limitations=["未穷尽所有路径"])
    assert DiscoveryOutput(limitations=["旧声明"]).limitations == ["旧声明"]


def test_gap_rejects_blank_fields():
    with pytest.raises(ValueError):
        DiscoveryGap(**{**gap(DECLARATION, ["head"]), "missing_fact": " \n "})


def test_structured_discovery_submission_round_trips_through_checkpoint_serializer():
    submission = DiscoverySubmission(limitations=[gap(DECLARATION, ["head"])])
    serializer = _checkpoint_serializer()
    restored = serializer.loads_typed(serializer.dumps_typed(submission))
    assert restored == submission and isinstance(restored.limitations[0], DiscoveryGap)


async def test_gap_goal_membership_uses_existing_submission_correction(source_service):
    output = DiscoverySubmission(limitations=[gap(DECLARATION, ["unknown"])])
    feedback = submission_feedback(output, [{"id": "head", "path": "safe.py", "side": "head",
        "start_line": 1, "end_line": 1, "status": "not_examined"}], source_service, set(), "assignment")
    data = json.loads(feedback.split("\n", 1)[1])
    assert data["invalid_gap_goal_ids"] == [0] and data["allowed_goal_ids"] == ["head"]


async def test_untrusted_gap_goal_ids_cannot_claim_another_scope(source_service):
    from app.services.publication.review_text_format import review_limitation_text

    _, report, _ = await execute(source_service)
    report.discovery_results[0].output["limitations"][0]["goal_ids"].append("outside")
    record = derive_review_outcome(task_for(report), evidence=source_service).review_limitations[0]
    assert "outside" not in record.goal_ids
    assert "outside" in record.gap.goal_ids
    assert "部分关联目标未通过范围校验" in review_limitation_text(record)


async def test_real_coverage_and_unresolved_candidates_still_affect_outcome(source_service):
    _, report, _ = await execute(source_service)
    report.complete = False
    outcome = derive_review_outcome(task_for(report), evidence=source_service)
    assert outcome.kind == "inconclusive" and "scope_incomplete" in outcome.reason_codes
    _, report, _ = await execute(source_service, ReviewModel(invalid_verifier=True))
    outcome = derive_review_outcome(task_for(report), evidence=source_service)
    assert outcome.kind == "inconclusive" and outcome.unresolved_count == 1
    assert "verification_unresolved" in outcome.reason_codes and not report.findings


async def test_unresolved_candidate_gap_does_not_imply_a_retired_assessment(source_service):
    from app.services.publication.review_text_format import review_limitation_text

    _, report, _ = await execute(source_service, ReviewModel(invalid_verifier=True))
    report.decisions[0].limitations = ["缺少调用方契约"]
    outcome = derive_review_outcome(task_for(report), evidence=source_service)
    assert outcome.kind == "inconclusive" and outcome.unresolved_count == 1
    assert outcome.pending_limitation_count == 0
    assert "verification_unresolved" in outcome.reason_codes
    assert "limitation_assessment_incomplete" not in outcome.reason_codes
    record = next(item for item in outcome.review_limitations if item.source == "verification")
    assert record.status == "reported" and "候选核验证据缺口" in review_limitation_text(record)


async def test_historical_report_keeps_its_policy_and_original_pending_gap(source_service):
    _, report, _ = await execute(source_service)
    raw = report.model_dump()
    raw.pop("policy_version")
    raw["discovery_results"][0]["output"]["limitations"] = [DECLARATION]
    raw["discovery_results"][0]["investigation_limitations"] = [DECLARATION]
    restored = ReviewReport.model_validate(raw)
    original = restored.model_dump_json()
    outcome = derive_review_outcome(task_for(restored), evidence=source_service)
    assert restored.policy_version == 1 and restored.model_dump_json() == original
    assert "policy_version" not in restored.model_dump()
    assert "gap" not in outcome.review_limitations[0].model_dump()
    from app.services.publication.review_text_format import review_limitation_text
    assert review_limitation_text(outcome.review_limitations[0]).startswith("待复核的限制声明：")
    assert outcome.kind == "inconclusive" and outcome.pending_limitation_count == 1
    assert "limitation_assessment_incomplete" in outcome.reason_codes


async def test_retired_role_cannot_be_built(source_service):
    with pytest.raises(ValueError, match="unknown_reviewer"):
        await asyncio.to_thread(build_agent, ReviewModel(), "limitation_reviewer", ReviewLimits())


def legacy_graph(saver, node, values):
    graph = StateGraph(ReviewState)
    graph.add_node("collect", lambda state: {})
    graph.add_node(node, lambda state: pytest.fail("retired node must never execute"))
    graph.add_edge(START, "collect")
    if node == "review_limitation":
        graph.add_conditional_edges("collect", lambda state: [
            Send(node, {"review_id": "review", "limitation_batch_id": "legacy-batch",
                        "review_limitations": [], "goals": values["goals"]})], [node])
    else:
        graph.add_edge("collect", node)
    graph.add_edge(node, END)
    return graph.compile(saver, interrupt_before=[node])


@pytest.mark.parametrize("node", ["plan_limitations", "review_limitation", "collect_limitations"])
async def test_sqlite_reopen_advances_retired_sends_without_model_replay(source_service, tmp_path, node):
    values, report, _ = await execute(source_service)
    config = {"configurable": {"thread_id": node}}
    values["report"] = {}
    values["limitation_batches"] = [{"id": "legacy-batch"}]
    database = tmp_path / f"{node}.sqlite"
    async with aiosqlite.connect(database) as connection:
        saver = AsyncSqliteSaver(connection, serde=_checkpoint_serializer())
        await legacy_graph(saver, node, values).ainvoke(values, config)
    model = GapModel()
    context = review_context(source_service, model)
    context.budget = ReviewBudget(context.limits)
    ticket = await context.budget.reserve("legacy-batch", 1, hold=1)
    before = await context.budget.summary()
    async with aiosqlite.connect(database) as connection:
        saver = AsyncSqliteSaver(connection, serde=_checkpoint_serializer())
        graph = build_review_graph(saver)
        state = await build_review_graph(saver, checkpoint_inspection=True).aget_state(config)
        assert state.next == (node,)
        state = await advance_retired_limitation_checkpoint(graph, config, state, context)
        assert state.next == ("evidence_gate",)
        resumed = await graph.ainvoke(None, config, context=context)
    restored = ReviewReport.model_validate(resumed["report"])
    assert model.requests == 0 and restored.termination == "completed"
    assert restored.discovery_results == report.discovery_results
    after = await context.budget.summary()
    assert after["model_calls"] == before["model_calls"] == 1
    assert after["outstanding_input_tokens"] > 0
    assert context.budget.rows[ticket]["input_tokens"] is None
    assert not context.budget.rows[ticket]["settled"]
    assert after["held_model_calls"] == 0
    await context.budget.settle(ticket, None)


async def test_historical_scope_candidate_still_needs_verification_and_publication_revalidation(source_service):
    from app.models.review.diff import ChangedFile
    from app.models.review_plan import ReviewPlanOptions
    from app.services.publication.review_publication import validated_findings
    from tests.review.test_native_graph import CHANGE

    values, report, _ = await execute(source_service, ReviewModel())
    original = values["discovery_results"][0]
    values["limitation_results"] = [{**original, "id": "legacy-batch",
        "output": {"findings": original["output"]["findings"], "assessments": []}}]
    values["discovery_results"] = [{**original, "output": {
        **original["output"], "findings": []}, "investigation_limitations": []}]
    for key in ("candidates", "decisions", "findings", "verification_results", "verification_batches"):
        values[key] = []
    values["report"] = {}
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "candidate"}}
    await legacy_graph(saver, "collect_limitations", values).ainvoke(values, config)
    graph = build_review_graph(saver)
    context = review_context(source_service, ReviewModel())
    state = await build_review_graph(saver, checkpoint_inspection=True).aget_state(config)
    await advance_retired_limitation_checkpoint(graph, config, state, context)
    resumed = await graph.ainvoke(None, config, context=context)
    restored = ReviewReport.model_validate(resumed["report"])
    assert len(restored.findings) == len(restored.verification_results) == 1
    assert restored.candidates[0].assignment_ids == ["legacy-batch"]
    assert restored.limitation_results[0].output == values["limitation_results"][0]["output"]
    task = task_for(restored)
    task.configuration = SimpleNamespace(options=ReviewPlanOptions())
    task.changed_files = [ChangedFile.model_validate({**CHANGE, "additions": 1, "deletions": 1})]
    findings, withheld = validated_findings(task, source_service.artifacts)
    assert findings == restored.findings and not withheld


async def test_completed_legacy_parallel_output_survives_an_interrupted_send(source_service, tmp_path):
    values, _, _ = await execute(source_service, ReviewModel())
    original = values["discovery_results"][0]
    historical = {**original, "id": "legacy-completed",
        "output": {"findings": original["output"]["findings"], "assessments": []}}
    values["discovery_results"] = [{**original, "output": {**original["output"], "findings": []}}]
    for key in ("candidates", "decisions", "findings", "verification_results", "verification_batches"):
        values[key] = []
    values.update(report={}, limitation_results=[], limitation_batches=[
        {"id": "legacy-completed"}, {"id": "legacy-interrupted"}])

    async def retired(state):
        if state["limitation_batch_id"] == "legacy-interrupted":
            interrupt("pending")
        return {"limitation_results": [historical]}

    old = StateGraph(ReviewState)
    old.add_node("collect", lambda state: {})
    old.add_node("review_limitation", retired)
    old.add_edge(START, "collect")
    old.add_conditional_edges("collect", lambda state: [
        Send("review_limitation", {"limitation_batch_id": identity})
        for identity in ("legacy-completed", "legacy-interrupted")], ["review_limitation"])
    old.add_edge("review_limitation", END)
    config = {"configurable": {"thread_id": "parallel-retired"}}
    path = tmp_path / "parallel.sqlite"
    async with aiosqlite.connect(path) as connection:
        saver = AsyncSqliteSaver(connection, serde=_checkpoint_serializer())
        await old.compile(saver).ainvoke(values, config, durability="sync")
    async with aiosqlite.connect(path) as connection:
        saver = AsyncSqliteSaver(connection, serde=_checkpoint_serializer())
        inspection = build_review_graph(saver, checkpoint_inspection=True)
        saved = await inspection.aget_state(config)
        assert saved.next == ("review_limitation",)
        assert saved.values["limitation_results"][0]["id"] == "legacy-completed"
        graph = build_review_graph(saver)
        context = review_context(source_service, ReviewModel())
        await advance_retired_limitation_checkpoint(graph, config, saved, context)
        restored_state = await graph.ainvoke(None, config, context=context)
    restored = ReviewReport.model_validate(restored_state["report"])
    assert restored.limitation_results[0].id == "legacy-completed"
    assert restored.candidates[0].assignment_ids == ["legacy-completed"]
    assert len(restored.findings) == len(restored.verification_results) == 1


@pytest.mark.parametrize("error", [asyncio.CancelledError, ReviewLeaseLost])
async def test_lease_or_cancellation_prevents_checkpoint_transition(source_service, error):
    async def stopped():
        raise error()

    context = review_context(source_service, GapModel(), check_active=stopped)
    graph = SimpleNamespace(aupdate_state=lambda *args, **kwargs: pytest.fail("must not write checkpoint"))
    with pytest.raises(error):
        await advance_retired_limitation_checkpoint(graph, {}, SimpleNamespace(
            next=("review_limitation",), tasks=(), values={}), context)


async def test_unknown_mixed_checkpoint_schedule_is_not_guessed(source_service):
    context = review_context(source_service, GapModel())
    with pytest.raises(ValueError, match="review_checkpoint_stage_changed"):
        await advance_retired_limitation_checkpoint(None, {}, SimpleNamespace(
            next=("review_limitation", "verify"), tasks=(), values={}), context)


async def test_retired_only_failure_can_close_without_resetting_history_or_usage(source_service):
    values, _, _ = await execute(source_service)
    failure = {"id": "legacy-batch", "status": "failed", "output": {}, "error": "RuntimeError",
               "model_calls": None, "tool_calls": None, "input_tokens": None, "output_tokens": None}
    values["limitation_results"] = [failure]
    update = retry_updates(values, ReviewLimits())
    assert not update["retry_results"] and "limitation_results" not in update
    values.update({key: value for key, value in update.items() if key not in {
        "discovery_results", "verification_results", "investigation_results"}})
    # Completed core results are reused; the failed historical attempt stays failed.
    model = GapModel()
    state = await build_review_graph().ainvoke(values, context=review_context(source_service, model))
    restored = ReviewReport.model_validate(state["report"])
    outcome = derive_review_outcome(task_for(restored), evidence=source_service)
    assert model.requests == 0 and restored.termination == "completed"
    assert restored.limitation_results[0].status == "failed"
    assert restored.limitation_results[0].model_calls is None
    assert outcome.kind == "clean" and outcome.runtime_reliability == "unknown"
    assert outcome.reason_codes == ["usage_integrity_unknown"]
    with pytest.raises(ValueError, match="review_retry_limit"):
        retry_updates(state, ReviewLimits())
