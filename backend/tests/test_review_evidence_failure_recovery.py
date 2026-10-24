import json
from copy import deepcopy

import pytest

from app.models.review import ReviewTask
from app.services.review.review_input_context import build_context_evidence
from app.services.review.review_run_budget import ReviewRepositoryRunBudgetStore, ReviewRunBudget
from app.services.review.review_runtime import EvidenceIdentityConflict, TaskReviewRuntime
from app.services.review.task_review_projection import project_task_runtime, runtime_failure_summary, _safe_diagnostic
from app.services.review.task_runtime_checkpoint import TaskRuntimeCheckpoint
from test_intent_response_recovery import slice_with_intent, valid
from tests.support.persistence import persistence as persistence
from test_r10_runtime_repairs import catalog_derivation
from test_task_runtime_p2 import configured as checkpoint_slice
from tests.support.runtime_harness import configured as derivation_slice, candidate, inspect_submit, verification
from tests.support.runtime_harness import make_slice


@pytest.mark.parametrize("reverse", [False, True])
def test_legacy_head_and_explicit_head_are_same_source(reverse):
    source = {"file_path": "src/a.py", "start_line": 1, "end_line": 1,
              "content": "return value", "content_hash": "hash", "head_sha": "head", "base_sha": "base"}
    catalogs = [{"e": source}, {"e": {**source, "side": "head"}}]
    if reverse:
        catalogs.reverse()
    assert catalog_derivation(catalogs)._all_evidence()["e"]["content"] == "return value"
    with pytest.raises(EvidenceIdentityConflict):
        catalog_derivation([{"e": source}, {"e": {**source, "side": "base"}}])._all_evidence()


async def test_head_reread_preserves_side_and_canonical_binding_after_restore(tmp_path, persistence):
    read = {"kind": "read", "tool": "file_read", "arguments": {
        "file_path": "src/a.py", "start_line": 1, "end_line": 1}, "purpose": "核对实现"}
    runtime, checkpoint, step, messages = await checkpoint_slice(
        tmp_path / "first", persistence, responses=[read])
    chunks, _ = build_context_evidence([{"file": "src/a.py", "content": "return value",
        "start_line": 1, "end_line": 1, "side": "head"}], "a" * 40, "b" * 40)
    chunk = chunks[0]
    identity = chunk["evidence_id"]
    change = {"goal_id": "t1", "old_start": 0, "old_length": 0, "new_start": 1, "new_length": 1,
              "lines": [{"old_line_no": None, "new_line_no": 1, "content": "return value"}]}
    original = {**chunk, "file_path": "src/a.py", "hunk_id": "t1", "canonical_change": change}
    step.executor.evidence_catalog[identity] = original
    step.executor.active_evidence_ids.add(identity)
    await runtime.advance_one({"w1": step})
    await runtime.advance_one({"w1": step})
    reread = step.executor.evidence_catalog[identity]
    assert reread["side"] == "head" and reread["canonical_change"] == change
    assert catalog_derivation([{"e": original}, {"e": reread}])._all_evidence()
    restored, _, restored_step, new_messages = await checkpoint_slice(
        tmp_path / "second", persistence, restore=True)
    assert restored_step.executor.evidence_catalog[identity] == reread
    assert not new_messages and len(messages) == 1
    assert restored_step.budget.calls_consumed == 1
    assert restored.snapshot()[0]["state"] == "ready"
    assert checkpoint.data["sessions"]["w1"]


async def test_head_read_never_relabels_existing_base_evidence(tmp_path):
    read = {"kind": "read", "tool": "file_read", "arguments": {
        "file_path": "src/a.py", "start_line": 1, "end_line": 1}, "purpose": "核对实现"}
    step, messages = make_slice(tmp_path, [read])
    runtime = TaskReviewRuntime([step.executor.work_item])
    await runtime.advance_one({"w1": step})
    chunks, _ = build_context_evidence([{"file": "src/a.py", "content": "return value",
        "start_line": 1, "end_line": 1}], "a" * 40, "b" * 40)
    identity = chunks[0]["evidence_id"]
    original = {**chunks[0], "file_path": "src/a.py", "side": "base"}
    step.executor.evidence_catalog[identity] = original
    with pytest.raises(EvidenceIdentityConflict):
        await runtime.advance_one({"w1": step})
    assert step.executor.evidence_catalog[identity] == original
    assert step.executor.work_item.state.value == "failed" and len(messages) == 1


def test_safe_internal_failure_keeps_category_and_controlled_location():
    runtime = TaskReviewRuntime()
    try:
        raise EvidenceIdentityConflict("token=private source-secret")
    except EvidenceIdentityConflict as exc:
        runtime.record_failure(exc, stage="task_advance")
    safe = _safe_diagnostic(runtime.failures[0])
    assert safe["category"] == "evidence_identity_conflict"
    assert safe["error_type"] == "EvidenceIdentityConflict"
    assert "private" not in json.dumps(safe) and "secret" not in json.dumps(safe)
    safe = _safe_diagnostic({"error_type": "untrusted-secret", "frames": [
        {"function": "_all_evidence", "line": 80, "secret": "private"},
        {"function": "untrusted-secret", "line": 1},
        {"function": ["untrusted-secret"], "line": 1},
        {"function": "refresh", "line": True}]})
    assert safe == {"frames": [{"function": "_all_evidence", "line": 80}]}
    assert "evidence_identity_conflict" in runtime_failure_summary([
        {"category": "model_response_rejected", "reason_codes": ["incomplete_goal_evidence"]},
        _safe_diagnostic(runtime.failures[0])])
    runtime.record_failure(ValueError("private"), stage="task_derivation")
    safe = _safe_diagnostic(runtime.failures[-1])
    assert "runtime_internal_error" in runtime_failure_summary([safe])
    assert "ValueError" in runtime_failure_summary([safe])
    assert "private" not in runtime_failure_summary([safe])
    safe = _safe_diagnostic({"reason_codes": ["private", "incomplete_goal_evidence", ["secret"]]})
    assert safe["reason_codes"] == ["incomplete_goal_evidence"]


def missing_citation(payload):
    response = valid(payload)
    response["examinations"][0]["evidence_ids"] = ["e1"]
    return response


@pytest.mark.parametrize("second,max_calls,termination", [
    (valid, 5, "plan_exhausted"),
    (missing_citation, 5, "model_response_rejected"),
    (valid, 1, "budget_exhausted"),
])
async def test_missing_citation_is_bounded_and_uses_original_budget(
        tmp_path, second, max_calls, termination):
    step, messages = slice_with_intent(tmp_path / "sdk", [missing_citation, second], max_calls=max_calls)
    runtime = TaskReviewRuntime([step.executor.work_item])
    assert await runtime.run({"w1": step}, deadline_seconds=10) == termination
    assert step.executor.work_item.format_corrections == 1
    assert step.budget.calls_consumed == len(messages) == min(2, max_calls)
    assert step.budget.settled_actual == 15 * len(messages)
    assert bool(step.executor.projection.examinations) is (termination == "plan_exhausted")
    if len(messages) == 2:
        feedback = json.loads(messages[1][1].content.splitlines()[-1])
        requirement = feedback["examination_evidence_requirements"][0]
        assert requirement == {"target_id": "t1", "file_path": "src/a.py",
            "start_line": 1, "end_line": 2, "side": "head", "visible_evidence_ids": ["e1", "e2"]}
        assert feedback["rejected_items"][0]["item"]["evidence_ids"] == ["e1"]
    report = project_task_runtime(runtime, {"w1": step}, list(step.executor.goals.values()),
                                  termination_reason=termination)
    if termination == "model_response_rejected":
        assert report.diagnostics[0]["reason_codes"] == ["incomplete_goal_evidence"]
        assert "未覆盖完整目标范围" in report.failure_summary


async def test_missing_source_cannot_be_repaired_by_citation_only(tmp_path):
    step, messages = slice_with_intent(tmp_path / "missing", [missing_citation])
    step.executor.active_evidence_ids.remove("e2")
    runtime = TaskReviewRuntime([step.executor.work_item])
    assert await runtime.run({"w1": step}, deadline_seconds=10) == "model_response_rejected"
    assert step.executor.work_item.format_corrections == 0
    assert not step.executor.projection.examinations and len(messages) == 1


def test_mixed_schema_and_coverage_rejections_keep_raw_indices_and_accepted_siblings(tmp_path):
    step, messages = slice_with_intent(tmp_path / "mixed", [])
    executor = step.executor
    executor.goals["t2"] = executor.goals["t1"].model_copy(update={"goal_id": "t2", "end_line": 1})
    executor.work_item.goal_ids.append("t2")
    executor.projection.pending_target_ids.add("t2")
    response = missing_citation({"author_targets": ["保留返回值。"]})
    accepted = deepcopy(response["examinations"][0])
    accepted["target_id"] = "t2"
    response["examinations"] = [{"target_id": "bad"}, accepted, response["examinations"][0]]
    first = step.apply_response(json.dumps(response))
    assert first["correction_pending"]
    assert next(error["index"] for error in first["errors"]
                if error["reason"] == "incomplete_goal_evidence") == 2
    assert [item["index"] for item in step.rejected_items] == [0, 2]
    accepted_result = executor.projection.examinations["t2"].model_copy(deep=True)
    step.repair_errors = []
    assert not step.apply_response(json.dumps(valid({"author_targets": ["保留返回值。"]})))["errors"]
    assert executor.projection.examinations["t2"] == accepted_result
    assert not executor.projection.pending_target_ids and not messages


async def test_schema_rejection_diagnostics_never_export_model_values(tmp_path):
    response = {"kind": "submit", "examinations": [{"target_id": "t1",
        "disposition": "private-model-value", "reason": "checked", "evidence_ids": ["e1"]}]}
    step, _ = slice_with_intent(tmp_path / "schema", [response, response])
    runtime = TaskReviewRuntime([step.executor.work_item])
    termination = await runtime.run({"w1": step}, deadline_seconds=10)
    report = project_task_runtime(runtime, {"w1": step}, list(step.executor.goals.values()),
                                  termination_reason=termination)
    assert termination == "model_response_rejected"
    assert report.diagnostics[0]["reason_codes"] == ["response_schema_invalid"]
    assert "private-model-value" not in json.dumps(report.diagnostics)


async def test_citation_correction_restores_without_duplicate_send(tmp_path, persistence):
    repository = persistence[0]
    repository.create_task(ReviewTask(id="run", pr_url="https://github.com/o/r/pull/1", engine="task_review_v1"))

    async def configure(name, responses):
        step, messages = slice_with_intent(tmp_path / name, responses)
        step.budget = await ReviewRunBudget.load_or_create("run", max_tokens=100000, max_calls=5,
            store=ReviewRepositoryRunBudgetStore(repository), recover_running=False)
        runtime = TaskReviewRuntime([step.executor.work_item])
        await TaskRuntimeCheckpoint(runtime, {"w1": step}, repository).restore()
        return runtime, step, messages

    runtime, step, first_messages = await configure("first", [missing_citation])
    await runtime.advance_one({"w1": step})
    assert step.repair_errors and not step.executor.projection.examinations
    runtime, step, second_messages = await configure("second", [valid])
    assert step.repair_errors and step.executor.work_item.format_corrections == 1
    assert await runtime.run({"w1": step}, deadline_seconds=10) == "plan_exhausted"
    runtime, step, final_messages = await configure("third", [])
    assert await runtime.run({"w1": step}, deadline_seconds=10) == "plan_exhausted"
    assert len(first_messages) == len(second_messages) == 1 and not final_messages
    assert step.budget.calls_consumed == 2 and step.budget.settled_actual == 30


@pytest.mark.parametrize("legacy_processed", [False, True])
@pytest.mark.parametrize("failure_point", ["catalog", "verification"])
async def test_candidate_conflict_does_not_poison_derivation_recovery(
        tmp_path, persistence, monkeypatch, legacy_processed, failure_point):
    repository = persistence[0]
    repository.create_task(ReviewTask(id="run", pr_url="https://github.com/o/r/pull/1", engine="task_review_v1"))

    async def configure(name, responses):
        folder = tmp_path / name
        folder.mkdir()
        runtime, steps, derivation, messages = derivation_slice(folder, responses)
        steps["w1"].budget = await ReviewRunBudget.load_or_create("run", max_tokens=100000, max_calls=5,
            store=ReviewRepositoryRunBudgetStore(repository), recover_running=False)
        checkpoint = TaskRuntimeCheckpoint(runtime, steps, repository)
        await checkpoint.restore()
        return runtime, steps, derivation, messages, checkpoint

    runtime, steps, derivation, messages, checkpoint = await configure("first", [inspect_submit([candidate()])])
    step = steps["w1"]
    # 使用真实的同身份异侧证据冲突，覆盖失败后的诊断与恢复；不抹掉安全拒绝。
    original = deepcopy(step.executor.evidence_catalog["e1"])
    with monkeypatch.context() as patch:
        def conflict(*args):
            return catalog_derivation([
                {"e1": original}, {"e1": {**original, "side": "base"}}])._all_evidence()
        patch.setattr(derivation, "_all_evidence" if failure_point == "catalog" else "_verify", conflict)
        with pytest.raises(EvidenceIdentityConflict):
            await runtime.advance_one(steps)
    candidate_id = next(iter(step.executor.projection.candidates))
    assert not derivation.data["processed_candidates"] and not derivation.data["candidates"]
    if legacy_processed:
        derivation.data["processed_candidates"].append(candidate_id)
    await checkpoint.persist()
    restored, restored_steps, derived, new_messages, _ = await configure("second", [verification])
    assert len(restored_steps) == 2 and len(derived.data["candidates"]) == 1
    assert await restored.run(restored_steps, deadline_seconds=10) == "plan_exhausted"
    assert len(messages) == len(new_messages) == 1
    assert restored_steps["w1"].budget.calls_consumed == 2
    assert derived.data["processed_candidates"] == [candidate_id]
    assert next(iter(derived.data["candidates"].values()))["status"] == "confirmed"
