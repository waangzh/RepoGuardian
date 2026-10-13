import asyncio
import time
import json
from copy import deepcopy

import pytest

from app.evaluation.runtime_experiments import (GROUPS, FIXED_SETTINGS, validate_experiment_plan,
                                              first_sample_gate, claim_experiment_slot, compare_fixed_scope,
                                              screen_empty_candidate_regression, compare_group_run)
from app.evaluation.data import sha256
from app.services.fingerprints import stable_hash
from app.services.review_runtime import TaskReviewRuntime
from app.services.task_runtime_checkpoint import TaskRuntimeCheckpoint
from app.services.review_run_budget import ReviewRunBudget, ReviewRepositoryRunBudgetStore
from test_task_review_sdk_slice import make_slice
from test_task_runtime_p2 import configured
from test_task_runtime_p3 import configured as derived_runtime, inspect_submit
from test_phase6a_persistence import persistence as persistence


def frozen_plan():
    payload = {"version": "runtime-experiments-v1", "lock_sha256": sha256(b"lock"),
        "diff_lock_sha256": sha256(b"diff"), "groups": GROUPS, "settings": FIXED_SETTINGS,
        "first_sample_id": "first", "sample_ids": ["first", "a", "b", "c", "d"],
        "screening": {"token_reduction": 0.25}}
    return {**payload, "plan_sha256": stable_hash(payload)}


@pytest.mark.parametrize("group", list(GROUPS))
def test_remaining_requires_exact_same_group_successful_first_gate(group):
    plan = frozen_plan()
    first_result = {"sample": "first"}
    gate = {"version": "runtime-first-gate-v1", "plan_sha256": plan["plan_sha256"], "group": group,
            "sample_id": "first", "result_sha256": stable_hash(first_result), "admitted": True,
            "fixed_scope_coverage": {"not_worse_than_baseline": True}}
    quality_payload = {"version": "runtime-quality-gate-v1", "plan_sha256": plan["plan_sha256"],
        "group": group, "sample_id": "first", "result_sha256": gate["result_sha256"],
        "adjudication_status": "complete", "quality_passed": True, "expand_allowed": True,
        "metrics": {"token_reduction": 0.25, "lost_baseline_defects": 0,
                     "new_false_positives": 0, "baseline_false_positives": 0,
                     "new_cannot_determine": 0}}
    quality = {**quality_payload, "gate_sha256": stable_hash(quality_payload)}
    validate_experiment_plan(plan, b"lock", b"diff", group, "first", ["first"])
    with pytest.raises(ValueError, match="independent quality gate with materials"):
        validate_experiment_plan(plan, b"lock", b"diff", group, "remaining", ["a", "b", "c", "d"], gate, quality)
    for bad in (None, {**gate, "admitted": False}, {**gate, "group": "wrong"}):
        with pytest.raises(ValueError, match="successful same-group first gate"):
            validate_experiment_plan(plan, b"lock", b"diff", group, "remaining", ["a", "b", "c", "d"], bad, quality)
    with pytest.raises(ValueError, match="first-one/remaining-four"):
        validate_experiment_plan(plan, b"lock", b"diff", group, "first", ["a"])
    changed = deepcopy(plan)
    changed["settings"]["repoguardian_run_max_tokens"] = 2_000_000
    changed["plan_sha256"] = stable_hash({key: value for key, value in changed.items() if key != "plan_sha256"})
    with pytest.raises(ValueError, match="frozen plan mismatch"):
        validate_experiment_plan(changed, b"lock", b"diff", group, "first", ["first"])


def test_missing_usage_or_scope_does_not_admit_expansion():
    plan = frozen_plan()
    plan["samples"] = {"first": {"sample": {}, "diff_input": {}, "readable_scope_hash": "scope",
                                "fixed_scope": []}}
    result = {"sample": {}, "diff_input": {}, "task_runtime_report": {"execution_status": "completed"},
              "task_runtime_configuration": {"experiment": GROUPS["protocol_180"]}}
    gate = first_sample_gate(result, plan, "protocol_180")
    assert not gate["admitted"] and "wire_usage_incomplete" in gate["errors"]
    assert "readable_scope_changed" in gate["errors"]
    assert not gate["quality_proven"] and not gate["default_switch_allowed"]


@pytest.mark.parametrize("versions", [None, {}, {"risk_planner_version": "unknown", "prompt_version": "task-review-prompt-v5"},
                                     {"risk_planner_version": "risk-planner-v3", "prompt_version": "unknown"}])
def test_invalid_explicit_runtime_versions_rejected_in_plan(versions):
    plan = frozen_plan()
    plan["runtime_versions"] = versions
    plan["plan_sha256"] = stable_hash({key: value for key, value in plan.items() if key != "plan_sha256"})
    if versions is None:
        # 历史计划依旧可离线验收；派发入口另行拒绝缺少版本的新运行。
        validate_experiment_plan(plan, b"lock", b"diff", "full_180", "first", ["first"])
    else:
        with pytest.raises(ValueError, match="runtime versions invalid"):
            validate_experiment_plan(plan, b"lock", b"diff", "full_180", "first", ["first"])


@pytest.mark.parametrize("field", ["prompt_version", "risk_planner_version"])
def test_first_gate_rejects_changed_runtime_versions(monkeypatch, field):
    plan = frozen_plan()
    plan["runtime_versions"] = {"prompt_version": "task-review-prompt-v5", "risk_planner_version": "risk-planner-v3"}
    plan["samples"] = {"first": {"sample": {}, "diff_input": {}, "readable_scope_hash": "scope", "fixed_scope": []}}
    result = {"sample": {}, "diff_input": {}, "task_runtime_report": {
        "execution_status": "completed", "readable_scope_hash": "scope", "scope_complete": True, "work_items": []},
        "task_runtime_configuration": {**plan["runtime_versions"], "experiment": GROUPS["full_180"],
                                       "provider": "deepseek", "model": "deepseek-flash"}}
    monkeypatch.setattr("app.evaluation.runtime_experiments.reconcile_wire_usage",
                        lambda value: {"integrity": "complete", "wire_attempts": 1})
    assert first_sample_gate(result, plan, "full_180")["admitted"]
    result["task_runtime_configuration"][field] = "old-version"
    assert first_sample_gate(result, plan, "full_180")["errors"] == ["runtime_versions_changed"]
    result["task_runtime_configuration"].pop(field)
    assert not first_sample_gate(result, plan, "full_180")["admitted"]


def test_group_comparison_rejects_changed_runtime_before_computing_savings(tmp_path):
    plan = frozen_plan()
    plan["runtime_versions"] = {"prompt_version": "task-review-prompt-v5", "risk_planner_version": "risk-planner-v3"}
    summary = {"experiment": {"plan_sha256": plan["plan_sha256"], "group": "full_180", "stage": "first"},
               "samples": [{"instance_id": "first", "status": "completed", "result_file": "0000.json"}]}
    (tmp_path / "run.json").write_text(json.dumps(summary), encoding="utf-8")
    (tmp_path / "0000.json").write_text(json.dumps({"task_runtime_configuration": {
        "prompt_version": "task-review-prompt-v5", "risk_planner_version": "risk-planner-v2"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="result runtime versions mismatch"):
        compare_group_run(plan, tmp_path, "full_180")


def test_fresh_output_cannot_reset_a_reserved_group_budget(tmp_path):
    plan = frozen_plan()
    claim_experiment_slot(plan, "protocol_180", "first", tmp_path / "registry", tmp_path / "output1")
    with pytest.raises(ValueError, match="already reserved"):
        claim_experiment_slot(plan, "protocol_180", "first", tmp_path / "registry", tmp_path / "output2")


def test_fixed_scope_comparison_keeps_missing_failed_and_unknown_targets_in_denominator():
    baseline = {"fixed_scope": [{"file_path": "a.py", "hunk_id": f"h{index}", "start_line": index,
        "end_line": index, "status": "reviewed"} for index in range(1, 4)]}
    result = {"task_runtime_report": {"initial_scope_coverage": [
        {"target_ref": "a.py", "start_line": 1, "end_line": 1, "status": "examined"},
        {"target_ref": "a.py", "start_line": 2, "end_line": 2, "status": "blocked"}]}}
    comparison = compare_fixed_scope(result, baseline)
    assert comparison["total_fixed_targets"] == 3 and comparison["examined"] == 1
    assert comparison["missing_targets"] == ["h3"]
    assert comparison["unexamined_ratio"] == 2 / 3 and not comparison["not_worse_than_baseline"]


def test_independent_valid_defect_lost_stops_screening_despite_examined_scope():
    packets = [{"blind_id": "b", "snapshot": {"repository": "repo", "head_sha": "sha"}}]
    adjudication = {"adjudication_status": "complete", "blind": True, "rejected": [],
                    "accepted": [{"blind_id": "b", "label": "valid_defect"}]}
    result = {"sample": {"instance_id": "sample", "repository": "repo", "head_sha": "sha"},
              "task_runtime_report": {"scope_complete": True, "findings": [], "execution_status": "completed",
                "work_items": [{"kind": "inspect", "state": "completed", "last_observation": {
                    "response_kind": "submit", "errors": [], "pending_goals": [], "correction_pending": False}}]},
              "model_calls": [{"raw_response": '{"kind":"submit","candidates":[]}'}]}
    screen = screen_empty_candidate_regression(result, packets, adjudication)
    assert screen["status"] == "failed" and not screen["expand_allowed"]
    assert screen["baseline_independent_valid_defects"] == 1 and screen["new_valid_defects"] == 0
    incomplete = {**adjudication, "blind": False}
    assert screen_empty_candidate_regression(result, packets, incomplete)["status"] == "not_established"
    result["model_calls"][0]["raw_response"] = "invalid"
    assert screen_empty_candidate_regression(result, packets, adjudication)["new_valid_defects"] is None


async def test_simplified_protocol_keeps_leads_visible_without_investigation(tmp_path):
    lead = {"proposition": "调用方是否兼容", "related_target_ids": ["t1"],
        "missing_evidence": ["调用方"], "evidence_requests": [{"file_path": "src/a.py", "start_line": 1, "end_line": 1}]}
    runtime, steps, derivation, messages = derived_runtime(tmp_path, [inspect_submit(leads=[lead])],
                                                          investigations_enabled=False)
    assert await runtime.run(steps, deadline_seconds=5) == "plan_exhausted"
    assert len(messages) == len(steps) == 1
    assert next(iter(derivation.data["leads"].values()))["reason"] == "experiment_investigations_disabled"


async def test_expired_unit_deadline_stops_new_dispatch_and_survives_restore(tmp_path, persistence):
    runtime, checkpoint, step, messages = await configured(tmp_path / "first", persistence)
    runtime.unit_timeout_seconds = 180
    runtime.unit_deadlines["u1"] = time.time() - 1
    await checkpoint.persist()
    assert await runtime.run({"w1": step}, deadline_seconds=5) == "experiment_unit_deadline"
    assert not messages and step.budget.calls_consumed == 0
    second = tmp_path / "second"
    second.mkdir()
    restored_step, restored_messages = make_slice(second, [])
    restored_step.executor.work_item.unit_id = "u1"
    restored_step.budget = await ReviewRunBudget.load_or_create("run", max_tokens=100_000, max_calls=5,
        store=ReviewRepositoryRunBudgetStore(persistence[0]), recover_running=False)
    restored = TaskReviewRuntime([restored_step.executor.work_item], unit_timeout_seconds=180)
    await TaskRuntimeCheckpoint(restored, {"w1": restored_step}, persistence[0]).restore()
    assert restored.unit_deadlines == runtime.unit_deadlines
    await restored.retry_unit("u1", {"w1": restored_step})
    assert await restored.run({"w1": restored_step}, deadline_seconds=5) == "experiment_unit_deadline"
    assert not restored_messages and restored_step.budget.calls_consumed == 0


async def test_unit_deadline_preserves_unknown_inflight_usage(tmp_path):
    step, _ = make_slice(tmp_path, [])
    class SDK:
        async def ainvoke(self, messages):
            await asyncio.Event().wait()
    step.provider._build_chat_model = lambda *args: SDK()
    runtime = TaskReviewRuntime([step.executor.work_item], unit_timeout_seconds=0.1)
    assert await runtime.run({"w1": step}, deadline_seconds=5) == "experiment_unit_deadline"
    assert step.budget.calls_consumed == 1 and step.budget.unknown_reserved > 0
    assert step.budget.settled_actual == 0


async def test_provider_timeout_is_not_mislabeled_as_unit_deadline(tmp_path):
    step, _ = make_slice(tmp_path, [])
    async def timeout():
        raise TimeoutError("provider")
    step.advance = timeout
    runtime = TaskReviewRuntime([step.executor.work_item], unit_timeout_seconds=180)
    with pytest.raises(TimeoutError):
        await runtime.advance_one({"w1": step})
    assert step.executor.work_item.state.value == "failed"
    assert step.executor.work_item.reason != "experiment_unit_deadline"
