import hashlib
import json
from copy import deepcopy
import asyncio

import pytest

from app.models.review_runtime import ReviewGoal, ReviewRunSpec, ReviewWorkItem, RuntimeReadArguments
from app.services.review_runtime import TaskReviewRuntime
from app.services.review_stability import CONTRACT, source_requests
from app.services.review_task_derivation import build_task_risk_catalog
from app.services.review_task_protocol import TaskReviewV1Protocol
from app.services.task_runtime_checkpoint import TaskRuntimeCheckpoint
from test_review_report_evidence import investigation_slice
from test_task_review_sdk_slice import make_slice
from test_phase6a_persistence import persistence as persistence


def enable(step):
    step.executor.spec = step.executor.spec.model_copy(update={"stability_contract": CONTRACT})
    step.protocol = TaskReviewV1Protocol(step.executor.spec)
    return step


def answer(payload, value="supported", missing=()):
    return {"kind": "submit", "investigation": {
        "answer": value, "evidence_ids": payload["active_evidence_ids"],
        "reason": "Checked the source for this proposition",
        "limitations": ["Source premise missing"] if missing else [],
        "missing_evidence": list(missing)}}


def nested_graph():
    return {"metadata": {"call_resolution_version": "import-evidence-v5"}, "symbols": [
        {"id": "tests/t.py::test", "file": "tests/t.py", "start_line": 1, "end_line": 2},
        {"id": "src/a.py::inner", "file": "src/a.py", "start_line": 5, "end_line": 6},
        {"id": "src/a.py::outer", "file": "src/a.py", "start_line": 3, "end_line": 4}],
        "edges": [{"type": "calls", "source": "tests/t.py::test", "target": f"src/a.py::{name}",
                   "source_file": "tests/t.py", "target_file": "src/a.py",
                   "resolution_status": "resolved", "resolution_basis": "python_lexical_import"}
                  for name in ["inner", "outer"]]}


def test_v7_plans_outer_and_inner_source_in_stable_order_and_preserves_v6():
    graph = nested_graph()
    goals = [ReviewGoal(goal_id="g", unit_id="u", kind="inspect", target_ref="tests/t.py",
                        start_line=1, end_line=2)]
    items = [ReviewWorkItem(work_item_id="w", kind="inspect", goal_ids=["g"])]
    old = build_task_risk_catalog(goals, items, graph, planner_version="risk-planner-v6")
    new = build_task_risk_catalog(goals, items, graph, planner_version="risk-planner-v7")
    assert all(len(risk["evidence_requests"]) == 3 for risk in new)
    assert all(len(risk["evidence_requests"]) == 2 for risk in old)
    graph["symbols"].reverse()
    graph["edges"].reverse()
    assert new == build_task_risk_catalog(goals, items, graph, planner_version="risk-planner-v7")


def test_preparation_keeps_whole_definitions_and_excludes_sensitive_paths():
    symbols = [{"file": "src/a.py", "start_line": 1, "end_line": 6},
               {"file": ".env", "start_line": 1, "end_line": 2}]
    requests, overflow = source_requests(symbols, {"src/a.py", ".env"}, 2, limit=2)
    assert requests == [] and overflow
    requests, overflow = source_requests(symbols, {"src/a.py", ".env"}, 2, limit=3)
    assert not overflow and len(requests) == 3
    assert {request.file_path for request in requests} == {"src/a.py"}


def test_sensitive_companion_source_is_a_real_gap_not_a_silently_removed_requirement():
    graph = nested_graph()
    graph["symbols"].append({"id": ".env::secret", "file": ".env", "start_line": 1, "end_line": 2})
    graph["edges"].append({**graph["edges"][0], "target": ".env::secret", "target_file": ".env"})
    goals = [ReviewGoal(goal_id="g", unit_id="u", kind="inspect", target_ref="tests/t.py",
                        start_line=1, end_line=2)]
    items = [ReviewWorkItem(work_item_id="w", kind="inspect", goal_ids=["g"])]
    risks = build_task_risk_catalog(goals, items, graph, planner_version="risk-planner-v7")
    assert risks and all(risk["not_scheduled_reason"] == "no_legal_evidence_path" for risk in risks)
    assert all(not risk["evidence_requests"] for risk in risks)


def test_v7_unverified_binding_remains_an_optional_hint_not_a_completed_investigation(tmp_path):
    from test_task_runtime_p3 import configured
    runtime, steps, derivation, _ = configured(tmp_path, [], risk_planner_version="risk-planner-v7")
    goal = derivation.initial_goals["t1"]
    graph = nested_graph()
    graph["symbols"][0]["file"] = goal.target_ref
    graph["symbols"][0]["id"] = goal.target_ref + "::test"
    for symbol in graph["symbols"][1:]:
        symbol.update(file="src/b.py", id=symbol["id"].replace("src/a.py", "src/b.py"))
    for edge in graph["edges"]:
        edge.update(source=goal.target_ref + "::test", source_file=goal.target_ref,
                    resolution_status="unresolved", target_file="src/b.py",
                    target=edge["target"].replace("src/a.py", "src/b.py"))
    risks = build_task_risk_catalog([goal], [steps["w1"].executor.work_item],
                                   graph, planner_version="risk-planner-v7")
    assert risks and all(risk.get("hint") for risk in risks)
    derivation.risk_catalog = risks
    derivation.refresh()
    from app.services.task_review_projection import project_task_runtime
    report = project_task_runtime(runtime, steps, [goal], termination_reason="running")
    assert report.relationship_hints and not report.admitted_investigations
    assert not report.unscheduled_leads


async def test_inspection_hydrates_definition_before_first_model_request(tmp_path):
    def submit(payload):
        assert any("return value" in item["content"] for item in payload["active_evidence"])
        return {"kind": "submit", "examinations": [{
            "target_id": "t1", "disposition": "examined", "evidence_ids": ["e1"],
            "reason": "Checked the target"}]}
    step, calls = make_slice(tmp_path, [submit])
    enable(step)
    step.symbol_index = [{"file": "src/a.py", "symbol": "f", "start_line": 1, "end_line": 2}]
    (tmp_path / "src/a.py").write_text("def f():\n    return value", encoding="utf-8")
    step.git.get_file_content_at_revision = lambda *args: "def f():\n    return value"
    assert (await step.advance())["status"] == "completed"
    assert calls == [] and step.budget.calls_consumed == 0
    assert not (await step.advance())["errors"]
    assert len(calls) == 1


async def test_required_outer_source_is_prepared_and_partial_support_rejected(tmp_path):
    step, calls = investigation_slice(tmp_path, [])
    enable(step)
    step.executor.work_item.evidence_requests = [
        RuntimeReadArguments(file_path="src/a.py", start_line=3, end_line=4)]
    partial = {"kind": "submit", "investigation": {"answer": "supported",
               "evidence_ids": ["e1", "e2"], "reason": "Unsupported outer conclusion"}}
    assert step.apply_response(json.dumps(partial))["errors"][0]["reason"] == "incomplete_investigation_evidence"
    assert step.executor.projection.investigation is None
    assert (await step.advance())["status"] == "completed"
    assert calls == []


async def test_structured_gap_recovery_roundtrips_checkpoint_without_rebilling(tmp_path):
    missing = {"file_path": "src/a.py", "start_line": 3, "end_line": 4}
    step, calls = investigation_slice(tmp_path, [
        lambda payload: answer(payload, "insufficient_evidence", [missing]),
        lambda payload: answer(payload)])
    enable(step)
    first = await step.advance()
    assert not first["errors"] and step.pending_read
    assert step.executor.projection.investigation is None
    saved = deepcopy(TaskRuntimeCheckpoint._session(step))
    TaskRuntimeCheckpoint._restore_session(step, saved)
    assert (await step.advance())["status"] == "completed"
    assert not (await step.advance())["errors"]
    assert step.executor.projection.investigation.answer.value == "supported"
    assert step.stability["recovery_rounds"] == 1
    assert len(calls) == 2 and step.budget.calls_consumed == 2
    assert step.budget.settled_actual == 30
    assert step.executor.projection.invalidated_results[0]["reason"] == "recoverable_source_gap"


@pytest.mark.parametrize("failure", ["stale", "dirty", "repeated"])
async def test_failed_or_repeated_recovery_keeps_insufficient_evidence(tmp_path, failure):
    missing = {"file_path": "src/a.py", "start_line": 3, "end_line": 4}
    step, calls = investigation_slice(tmp_path, [
        lambda payload: answer(payload, "insufficient_evidence", [missing]),
        lambda payload: answer(payload, "insufficient_evidence", [missing])])
    enable(step)
    await step.advance()
    if failure == "stale":
        step.git._run = lambda *args: "f" * 40
    elif failure == "dirty":
        (tmp_path / "src/a.py").write_text("changed\n" * 6, encoding="utf-8")
    await step.advance()
    await step.advance()
    assert step.pending_read is None
    assert step.executor.projection.investigation.answer.value == "insufficient_evidence"
    assert step.stability["recovery_rounds"] == 1 and len(calls) == 2


@pytest.mark.parametrize("path", [".env", "private.py"])
def test_structured_gap_cannot_expand_frozen_read_permissions(tmp_path, path):
    step, _ = investigation_slice(tmp_path, [])
    enable(step)
    raw = answer({"active_evidence_ids": ["e1", "e2"]}, "insufficient_evidence",
                 [{"file_path": path, "start_line": 1, "end_line": 2}])
    assert step.apply_response(json.dumps(raw))["errors"][0]["reason"] == "read_path_outside_work_item_scope"
    assert step.pending_read is None and step.executor.projection.investigation is None


def test_legacy_prompt_schema_and_spec_do_not_expose_new_contract(tmp_path):
    step, _ = investigation_slice(tmp_path, [])
    assert "stability_contract" not in step.executor.spec.model_dump()
    old = step.protocol.prepare(step.executor.work_item, step.executor.current_goals(),
        active_evidence_ids=step.executor.active_evidence_ids, evidence_catalog=step.executor.evidence_catalog)
    assert "missing_evidence" not in json.loads(old.prompt)["response"]["one_of"][1][
        "$defs"]["InvestigationSubmission"]["properties"]
    assert "missing_evidence" in json.loads(old.prompt)["response"]["one_of"][1][
        "$defs"]["LeadSubmission"]["properties"]
    assert old.temperature == 0.1
    raw = answer({"active_evidence_ids": ["e1", "e2"]}, "insufficient_evidence")
    assert step.apply_response(json.dumps(raw))["errors"][0]["reason"] == "unknown_submit_field"
    legacy = ReviewRunSpec.model_validate(step.executor.spec.model_dump())
    assert legacy.stability_contract is None


async def test_low_temperature_matches_actual_request_envelope(tmp_path):
    step, calls = make_slice(tmp_path, [{"kind": "submit", "examinations": [{
        "target_id": "t1", "disposition": "examined", "evidence_ids": ["e1"], "reason": "Checked"}]}])
    enable(step)
    sdk = step.provider._build_chat_model(None, 4096)
    step.provider._build_chat_model = lambda *args: sdk
    request = step.protocol.prepare(step.executor.work_item, step.executor.current_goals(),
        active_evidence_ids={"e1"}, evidence_catalog=step.executor.evidence_catalog)
    assert json.loads(request.envelope())["temperature"] == 0
    await step.advance()
    assert sdk.temperature == 0 and len(calls) == 1


async def test_last_call_requests_submission_and_does_not_execute_model_search(tmp_path):
    step, calls = make_slice(tmp_path, [{"kind": "read", "tool": "file_find",
        "arguments": {"query": "*.py"}, "purpose": "More exploration"}], max_calls=1)
    enable(step)
    runtime = TaskReviewRuntime([step.executor.work_item])
    result = await runtime.advance_one({"w1": step})
    payload = json.loads(calls[0][1].content)
    assert payload["finalization"] and len(payload["response"]["one_of"]) == 1
    assert "read_example" not in payload
    assert result["errors"] and step.pending_read is None
    assert not step.executor.projection.examinations
    assert runtime.snapshot()[0]["state"] == "waiting_evidence"


async def test_explicit_retry_preserves_accepted_targets_and_reopens_only_investigation(tmp_path):
    step, _ = investigation_slice(tmp_path, [])
    enable(step)
    step.apply_response(json.dumps(answer({"active_evidence_ids": ["e1", "e2"]}, "insufficient_evidence")))
    step.executor.work_item.unit_id = "u1"
    runtime = TaskReviewRuntime([step.executor.work_item])
    from app.models.review_runtime import WorkItemState
    step.executor.work_item.state = WorkItemState.completed
    assert await runtime.retry_unit("u1", {"w1": step}) == ["w1"]
    assert step.executor.projection.investigation is None
    assert step.executor.projection.pending_target_ids == {"t1"}
    assert step.budget.calls_consumed == 0


def test_new_checkpoint_requires_stability_state(tmp_path):
    step, _ = investigation_slice(tmp_path, [])
    enable(step)
    saved = TaskRuntimeCheckpoint._session(step)
    saved.pop("stability")
    with pytest.raises(ValueError, match="stability_checkpoint_missing"):
        TaskRuntimeCheckpoint._restore_session(step, saved)


def test_source_content_remains_hash_bound(tmp_path):
    step, _ = investigation_slice(tmp_path, [])
    enable(step)
    evidence = step.executor.evidence_catalog["e1"]
    assert hashlib.sha256(evidence["content"].encode()).hexdigest() == evidence["content_hash"]
    evidence["content"] += "\nforged"
    assert step.executor.evidence_error(["e1"]) == "evidence_content_hash_mismatch"


async def test_recovery_stops_after_two_rounds_without_forcing_a_positive_answer(tmp_path):
    ranges = [{"file_path": "src/a.py", "start_line": start, "end_line": start + 1}
              for start in (3, 7, 9)]
    step, calls = investigation_slice(tmp_path, [
        lambda payload, request=request: answer(payload, "insufficient_evidence", [request])
        for request in ranges])
    enable(step)
    source = (tmp_path / "src/a.py").read_text("utf-8") + "\nextra1\nextra2\nextra3\nextra4"
    (tmp_path / "src/a.py").write_text(source, encoding="utf-8")
    step.git.get_file_content_at_revision = lambda *args: source
    for _ in range(5):
        await step.advance()
    assert step.executor.projection.investigation.answer.value == "insufficient_evidence"
    assert step.stability["recovery_rounds"] == 2 and step.pending_read is None
    assert len(calls) == 3 and step.budget.calls_consumed == 3


async def test_cancelled_runtime_never_runs_preparatory_reads(tmp_path):
    step, calls = investigation_slice(tmp_path, [])
    enable(step)
    step.executor.work_item.evidence_requests = [
        RuntimeReadArguments(file_path="src/a.py", start_line=3, end_line=4)]
    runtime = TaskReviewRuntime([step.executor.work_item])
    await runtime.cancel({"w1": step})
    assert await runtime.advance_one({"w1": step}) is None
    assert calls == [] and not step.store.entries
    assert step.executor.projection.investigation is None


@pytest.mark.parametrize("boundary", ["response_saved", "recovery_applied"])
async def test_durable_recovery_crash_replays_once_without_duplicate_model_call(tmp_path, persistence, boundary):
    from app.models.review import ReviewTask
    from app.services.review_run_budget import ReviewRepositoryRunBudgetStore, ReviewRunBudget
    repository = persistence[0]
    repository.create_task(ReviewTask(id="run", pr_url="https://github.com/a/b/pull/1",
                                     engine="task_review_v1"))
    missing = {"file_path": "src/a.py", "start_line": 3, "end_line": 4}

    async def prepare(path, responses):
        path.mkdir()
        step, calls = investigation_slice(path, responses)
        enable(step)
        step.budget = await ReviewRunBudget.load_or_create(
            "run", max_tokens=100_000, max_calls=5,
            store=ReviewRepositoryRunBudgetStore(repository), recover_running=False)
        runtime = TaskReviewRuntime([step.executor.work_item])
        checkpoint = TaskRuntimeCheckpoint(runtime, {"w1": step}, repository)
        await checkpoint.restore()
        return runtime, checkpoint, step, calls

    runtime, checkpoint, step, calls = await prepare(tmp_path / "first", [
        lambda payload: answer(payload, "insufficient_evidence", [missing])])
    original = checkpoint.save_response if boundary == "response_saved" else checkpoint.applied

    async def crash(*args):
        await original(*args)
        raise asyncio.CancelledError()

    if boundary == "response_saved":
        checkpoint.save_response = crash
    else:
        checkpoint.applied = crash
    with pytest.raises(asyncio.CancelledError):
        await runtime.advance_one({"w1": step})
    restored, _, recovered, new_calls = await prepare(tmp_path / "second", [
        lambda payload: answer(payload)])
    assert len(calls) == 1 and not new_calls
    assert recovered.stability["recovery_rounds"] == 1 and recovered.pending_read
    assert await restored.run({"w1": recovered}, deadline_seconds=10) == "plan_exhausted"
    assert recovered.executor.projection.investigation.answer.value == "supported"
    assert len(new_calls) == 1 and recovered.budget.calls_consumed == 2
    assert recovered.budget.settled_actual == 30


async def test_retry_api_accepts_unresolved_investigation_with_complete_initial_coverage(
        persistence, monkeypatch):
    from app.models.review import ReviewTask
    from app.services.review_service import ReviewService
    repository, queue, _ = persistence
    task = ReviewTask(id="run", pr_url="https://github.com/a/b/pull/1", engine="task_review_v1",
        status="completed_with_warnings", task_runtime_configuration={"stability_contract": CONTRACT},
        task_runtime_report={"execution_status": "completed", "termination_reason": "plan_exhausted",
            "scope_complete": True, "initial_scope_coverage": [
                {"goal_id": "g", "unit_id": "u", "status": "examined"}],
            "admitted_investigations": [{"work_item_id": "i", "unit_id": "u",
                "status": "insufficient_evidence", "answer": "insufficient_evidence"}],
            "work_items": [{"work_item_id": "i", "unit_id": "u", "state": "completed",
                           "kind": "investigate", "goal_ids": ["investigation-g"]}]})
    repository.create_task(task)
    service = ReviewService(None, None, None, None, None, repository=repository, task_queue=queue)
    monkeypatch.setattr(service, "_ensure_worker_started", lambda: None)
    result = await service.retry_unit("run", "u")
    assert result.requeued_work_item_ids == ["i"]
    saved = repository.get_task("run")
    assert saved.status.value == "queued"
    assert saved.task_runtime_report["initial_scope_coverage"][0]["status"] == "examined"
