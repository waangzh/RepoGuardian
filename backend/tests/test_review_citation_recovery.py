import json
from copy import deepcopy

import pytest

from app.models.review_runtime import ReviewGoal, ReviewWorkItem, RuntimeReadArguments, WorkItemState
from app.services.review.review_runtime import TaskReviewRuntime
from app.services.review.review_task_derivation import build_task_risk_catalog
from app.services.review.task_review_projection import project_task_runtime, runtime_failure_summary, task_runtime_summary
from app.services.review.task_runtime_checkpoint import TaskRuntimeCheckpoint
from test_python_call_binding import indexed_graph
from test_review_report_evidence import investigation_slice
from test_review_stability import enable


@pytest.mark.parametrize("body,needed", [
    ("    unrelated()\n    return measure(inner())\n", {1, 3}),
    ("    unrelated(); return measure(inner())\n", {1, 3}),
    ("    unrelated(inner()); return measure(inner())\n", {1, 3}),
    ("    unrelated()\n    return measure(\n        inner(),\n    )\n", {1, 3}),
    ("    return measure(inner(), unrelated())\n", {1, 3, 5}),
])
def test_v8_required_implementations_follow_nested_sites_not_all_caller_calls(tmp_path, body, needed):
    graph, _ = indexed_graph(tmp_path,
        "from pkg.stats import measure, inner, unrelated\ndef run():\n" + body,
        target_source="def measure(value):\n    return value\ndef inner():\n    return 1\ndef unrelated(*args):\n    return 2\n",
        version="import-evidence-v6")
    goals = [ReviewGoal(goal_id="g", unit_id="u", kind="inspect", target_ref="src/pkg/stats.py",
                        start_line=1, end_line=2)]
    items = [ReviewWorkItem(work_item_id="w", kind="inspect", goal_ids=["g"])]

    def plan(value, version):
        risks = build_task_risk_catalog(goals, items, value, planner_version=version)
        return next(risk for risk in risks if risk.get("relationship", {}).get("target")
                    == "src/pkg/stats.py::measure")

    risk = plan(graph, "risk-planner-v8")
    ranges = risk["evidence_requests"]
    assert not risk["not_scheduled_reason"]
    assert {req["start_line"] for req in ranges if req["file_path"] == "src/pkg/stats.py"} == needed
    assert any(req["file_path"] == "caller.py" for req in ranges)
    graph["symbols"].reverse()
    graph["edges"].reverse()
    assert plan(graph, "risk-planner-v8") == risk

    # v7 keeps its original obligations, even with richer source metadata.
    graph["metadata"]["call_resolution_version"] = "import-evidence-v5"
    assert {req["start_line"] for req in plan(graph, "risk-planner-v7")["evidence_requests"]
            if req["file_path"] == "src/pkg/stats.py"} == {1, 3, 5}
    graph["metadata"]["call_resolution_version"] = "import-evidence-v6"
    next(edge for edge in graph["edges"] if edge["type"] == "calls")["call_sites"][0].pop("call_range")
    assert {req["start_line"] for req in plan(graph, "risk-planner-v8")["evidence_requests"]
            if req["file_path"] == "src/pkg/stats.py"} == {1, 3, 5}


def test_call_sites_preserve_repeated_calls_on_same_line_and_old_graph_shape(tmp_path):
    source = "from pkg.stats import measure\ndef run():\n    return measure(measure(1))\n"
    modern, _ = indexed_graph(tmp_path, source, version="import-evidence-v6")
    calls = next(edge for edge in modern["edges"] if edge["type"] == "calls")["call_sites"]
    assert len(calls) == 2 and calls[0]["call_range"] != calls[1]["call_range"]
    old, _ = indexed_graph(tmp_path, source, version="import-evidence-v5")
    sites = next(edge for edge in old["edges"] if edge["type"] == "calls")["call_sites"]
    assert len(sites) == 1 and "call_range" not in sites[0]


def test_v8_inner_call_still_requires_the_outer_consumer_implementation(tmp_path):
    graph, _ = indexed_graph(tmp_path,
        "from pkg.stats import measure, inner, unrelated\ndef run():\n"
        "    unrelated()\n    return measure(\n        inner(),\n    )\n",
        target_source="def measure(value):\n    return value\ndef inner():\n    return 1\ndef unrelated():\n    return 2\n",
        version="import-evidence-v6")
    goals = [ReviewGoal(goal_id="g", unit_id="u", kind="inspect", target_ref="src/pkg/stats.py",
                        start_line=3, end_line=4)]
    items = [ReviewWorkItem(work_item_id="w", kind="inspect", goal_ids=["g"])]
    risk = next(risk for risk in build_task_risk_catalog(goals, items, graph, planner_version="risk-planner-v8")
                if risk.get("relationship", {}).get("target") == "src/pkg/stats.py::inner")
    assert {req["start_line"] for req in risk["evidence_requests"]
            if req["file_path"] == "src/pkg/stats.py"} == {1, 3}

    # An inaccessible nested consumer is a real gap; it cannot be dropped as unrelated.
    next(symbol for symbol in graph["symbols"] if symbol["id"].endswith("::measure"))["file"] = ".env"
    risk = next(risk for risk in build_task_risk_catalog(goals, items, graph, planner_version="risk-planner-v8")
                if risk.get("relationship", {}).get("target") == "src/pkg/stats.py::inner")
    assert risk["not_scheduled_reason"] == "no_legal_evidence_path" and not risk["evidence_requests"]


def citation_slice(tmp_path, responses, **kwargs):
    step, messages = investigation_slice(tmp_path, responses, **kwargs)
    enable(step)
    step.executor.active_evidence_ids.add("archived")
    step.executor.work_item.evidence_requests = [
        RuntimeReadArguments(file_path="src/a.py", start_line=3, end_line=4)]
    return step, messages


def submission(ids):
    return {"kind": "submit", "investigation": {
        "answer": "supported", "reason": "Checked the consumer implementation", "evidence_ids": ids}}


@pytest.mark.parametrize("corrected,max_calls,termination", [
    (True, 5, "plan_exhausted"),
    (False, 5, "model_response_rejected"),
    (True, 1, "budget_exhausted"),
])
async def test_visible_but_uncited_source_gets_precise_bounded_correction_after_restore(
        tmp_path, corrected, max_calls, termination):
    partial = submission(["e1", "e2"])
    step, messages = citation_slice(tmp_path, [partial,
        submission(["e1", "e2", "archived"]) if corrected else partial], max_calls=max_calls)
    item = step.executor.work_item
    runtime = TaskReviewRuntime([item])
    steps = {item.work_item_id: step}
    first = await runtime.advance_one(steps)
    assert first["correction_pending"]
    assert step.executor.projection.investigation is None
    saved = deepcopy(TaskRuntimeCheckpoint._session(step))
    TaskRuntimeCheckpoint._restore_session(step, saved)
    assert await runtime.run(steps, deadline_seconds=10) == termination
    assert item.format_corrections == 1
    assert step.budget.calls_consumed == len(messages) == min(2, max_calls)
    assert step.budget.settled_actual == 15 * len(messages)
    assert bool(step.executor.projection.investigation) == (termination == "plan_exhausted")
    if len(messages) == 2:
        feedback = next(json.loads(line) for line in messages[1][1].content.splitlines()[1:]
                        if "investigation_citation_correction" in line)["investigation_citation_correction"]
        assert feedback["status"] == "ready"
        assert feedback["missing_required_evidence_ids"] == []
        assert feedback["uncovered_source_ranges"] == [{"file_path": "src/a.py",
            "start_line": 3, "end_line": 4, "visible_evidence_ids": ["archived"]}]
        assert set(feedback["citation_ids"]) == {"e1", "e2", "archived"}
        assert "not proof" in feedback["instruction"]


@pytest.mark.parametrize("failure", ["inactive", "stale", "bad_hash"])
def test_citation_feedback_never_offers_unavailable_or_invalid_evidence(tmp_path, failure):
    step, _ = citation_slice(tmp_path, [])
    executor = step.executor
    if failure == "inactive":
        executor.active_evidence_ids.remove("archived")
    elif failure == "stale":
        executor.evidence_catalog["archived"]["head_sha"] = "f" * 40
    else:
        executor.evidence_catalog["archived"]["content_hash"] = "bad"
    feedback = executor.investigation_citation_gaps(["e1", "e2"])
    assert feedback["status"] != "ready" and feedback["citation_ids"] is None


def test_correction_separates_missing_required_ids_from_uncovered_ranges(tmp_path):
    step, _ = citation_slice(tmp_path, [])
    feedback = step.executor.investigation_citation_gaps(["e1", "archived"])
    assert feedback["missing_required_evidence_ids"] == ["e2"]
    assert feedback["uncovered_source_ranges"] == []


@pytest.mark.parametrize("state,started,status", [
    ("ready", False, "not_executed"), ("running", True, "incomplete"),
    ("cancelled", True, "incomplete"), ("failed", True, "failed"),
])
def test_projection_distinguishes_failed_started_and_never_started_investigations(
        tmp_path, state, started, status):
    step, _ = citation_slice(tmp_path, [])
    item = step.executor.work_item
    item.state, item.started = WorkItemState(state), started
    runtime = TaskReviewRuntime([item])
    report = project_task_runtime(runtime, {item.work_item_id: step}, [], termination_reason="running")
    assert report.admitted_investigations[0]["status"] == status
    summary = task_runtime_summary(report)["investigations"]
    assert summary[status] == 1 and summary["answered"] == 0
    assert summary["not_executed"] == int(not started)


def test_incomplete_investigation_has_actionable_failure_summary():
    message = runtime_failure_summary([{"category": "model_response_rejected",
        "reason_codes": ["incomplete_investigation_evidence"]}])
    assert "调查提交的证据引用未覆盖全部必要范围" in message
    assert "incomplete_investigation_evidence" in message
