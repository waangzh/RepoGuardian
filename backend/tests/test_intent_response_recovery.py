import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from app.graph.nodes.task_review_runtime import frozen_runtime_spec, task_review_runtime_node
from app.graph.nodes.report import report_node
from app.models.review import ReviewTask
from app.models.review_runtime import ReviewGoal
from app.services.publication_planner import PublicationPlanner
from app.services.model_request_budgeter import ModelRequestProfile
from app.services.review_run_budget import ReviewRunBudget, ReviewRepositoryRunBudgetStore
from app.services.review_runtime import TaskReviewRuntime
from app.services.review_task_protocol import TaskReviewV1Protocol
from app.services.task_review_projection import project_task_runtime, runtime_failure_summary
from app.services.task_runtime_checkpoint import TaskRuntimeCheckpoint
from test_phase6a_persistence import persistence as persistence
from test_review_outcome import completed_task
from test_task_review_sdk_slice import make_slice
from test_task_runtime_p4 import production_input as production_input


def slice_with_intent(folder, responses, *, max_calls=5, contract="structured-intent-v2"):
    folder.mkdir(parents=True)
    step, messages = make_slice(folder, responses, max_calls=max_calls)
    step.provider._profile_override = ModelRequestProfile(context_window=65536)
    executor = step.executor
    executor.spec = executor.spec.model_copy(update={"prompt_version": "task-review-prompt-v6",
        "pr_intent_context": "保留返回值。\n拒绝空值。", "pr_intent_context_truncated": False,
        "intent_assessment_contract": contract})
    executor.goals["t1"] = executor.goals["t1"].model_copy(update={"end_line": 2})
    executor.evidence_catalog["e2"] = {**executor.evidence_catalog["e1"], "start_line": 2, "end_line": 2,
        "content": "return other", "content_hash": hashlib.sha256(b"return other").hexdigest()}
    executor.active_evidence_ids.add("e2")
    step.protocol = TaskReviewV1Protocol(executor.spec)
    return step, messages


def valid(payload):
    return {"kind": "submit", "examinations": [{"target_id": "t1", "disposition": "examined",
        "reason": "已检查整段代码", "evidence_ids": ["e1", "e2"], "intent_assessments": [{
            "author_quote": payload["author_targets"][0], "actual_behavior": "局部返回值保持不变",
            "alignment": "supported", "evidence_ids": ["e1"], "limitations": ["仅当前局部观察"]}]}]}


def invalid(payload):
    value = valid(payload)
    value["examinations"][0]["intent_assessments"][0]["author_quote"] = "return value"
    return value


def payload_for(step):
    e = step.executor
    return json.loads(step.protocol.prepare(e.work_item, e.current_goals(), active_evidence_ids=e.active_evidence_ids,
        evidence_catalog=e.evidence_catalog).prompt)


def test_new_schema_enumerates_only_frozen_author_claims_and_keeps_old_contract(tmp_path):
    step, _ = slice_with_intent(tmp_path / "new", [])
    payload = payload_for(step)
    quote = payload["response"]["one_of"][1]["$defs"]["IntentAssessment"]["properties"]["author_quote"]
    assert quote["enum"] == payload["author_targets"] == ["保留返回值。", "拒绝空值。"]
    assert "intent_assessments" in payload["submit_shape_example"]["examinations"][0]
    old, _ = slice_with_intent(tmp_path / "old", [], contract="structured-intent-v1")
    assert "enum" not in payload_for(old)["response"]["one_of"][1]["$defs"]["IntentAssessment"]["properties"]["author_quote"]
    state = {"task_id": "run", "head_sha": "a" * 40, "effective_base_sha": "b" * 40,
        "pr_info": {"title": "保留返回值。"}}
    config = {"provider": "openai-compatible", "model": "fixture", "policy_version": "p1"}
    for version, expected in [("v1", "structured-intent-v1"), ("v2", "structured-intent-v1"), ("v3", "structured-intent-v2")]:
        spec = frozen_runtime_spec(state, {**config, "pr_intent_contract": "pr-intent-assessment-" + version},
            SimpleNamespace(max_calls=5, max_tokens=100000))
        assert spec.intent_assessment_contract == expected


@pytest.mark.parametrize("contract,expected", [("structured-intent-v1", "intent_evidence_scope_mismatch"), ("structured-intent-v2", None)])
def test_local_observation_and_full_examination_coverage_are_separate(tmp_path, contract, expected):
    step, _ = slice_with_intent(tmp_path / contract, [], contract=contract)
    result = step.executor.parse_and_apply(valid(payload_for(step)))
    assert [error.reason for error in result.errors] == ([expected] if expected else [])
    assert bool(step.executor.projection.examinations) is (expected is None)


@pytest.mark.parametrize("case,reason", [("quote", "unknown_author_target"),
    ("missing", "missing_intent_assessment"), ("foreign", "intent_evidence_outside_examination"),
    ("empty", "intent_evidence_missing"), ("blocked", "intent_examination_not_examined"),
    ("range", "intent_evidence_scope_mismatch"), ("file", "intent_evidence_scope_mismatch"),
    ("side", "intent_evidence_scope_mismatch"), ("whole_range", "incomplete_goal_evidence"),
    ("stale", "stale_evidence_snapshot"), ("tampered", "evidence_content_hash_mismatch")])
def test_binding_rejections_remain_precise_and_conservative(tmp_path, case, reason):
    step, _ = slice_with_intent(tmp_path / case, [])
    response = valid(payload_for(step))
    item = response["examinations"][0]
    assessment = item["intent_assessments"][0]
    if case == "quote":
        assessment["author_quote"] = "return value"
    elif case == "missing":
        item["intent_assessments"] = []
    elif case == "foreign":
        assessment["evidence_ids"] = ["foreign"]
    elif case == "empty":
        assessment["evidence_ids"] = []
    elif case == "blocked":
        item["disposition"] = "blocked"
    elif case == "whole_range":
        item["evidence_ids"] = ["e1"]
    elif case == "stale":
        step.executor.evidence_catalog["e1"]["head_sha"] = "c" * 40
    elif case == "tampered":
        step.executor.evidence_catalog["e1"]["content"] = "tampered"
    else:
        evidence = deepcopy(step.executor.evidence_catalog["e1"])
        if case == "range":
            evidence.update(start_line=3, end_line=3)
        if case == "file":
            evidence["file_path"] = "src/b.py"
            step.executor.work_item.scope_files.append("src/b.py")
        if case == "side":
            evidence["side"] = "base"
            step.executor.goals["base"] = ReviewGoal(goal_id="base", unit_id="u1", kind="inspect",
                target_ref="src/a.py", target_kind="base_source_range")
        step.executor.evidence_catalog["extra"] = evidence
        step.executor.active_evidence_ids.add("extra")
        item["evidence_ids"].append("extra")
        assessment["evidence_ids"] = ["extra"]
    result = step.executor.parse_and_apply(response)
    assert [error.reason for error in result.errors] == [reason]
    assert not step.executor.projection.examinations


async def test_single_correction_fixes_real_failure_shape_with_shared_budget(tmp_path):
    step, messages = slice_with_intent(tmp_path / "sdk", [invalid, valid])
    runtime = TaskReviewRuntime([step.executor.work_item])
    assert await runtime.run({"w1": step}, deadline_seconds=10) == "plan_exhausted"
    assert len(messages) == step.budget.calls_consumed == 2
    assert step.budget.settled_actual == 30 and step.executor.work_item.format_corrections == 1
    feedback = json.loads(messages[1][1].content.splitlines()[-1])
    assert "unknown_author_target" in feedback["intent_correction"]
    assert feedback["rejected_items"][0]["item"]["intent_assessments"][0]["author_quote"] == "return value"
    assert step.executor.projection.examinations["t1"].intent_assessments[0].evidence_ids == ["e1"]
    report = project_task_runtime(runtime, {"w1": step}, list(step.executor.goals.values()), termination_reason="plan_exhausted")
    task = completed_task(**report.model_dump(mode="json"))
    task.run_manifest.effective_base_sha = step.executor.spec.snapshot_base_sha
    assert PublicationPlanner().build(task).digest.intent_assessments[0].status == "observed"


async def test_repeated_rejection_has_safe_diagnostic_and_incomplete_failed_result(tmp_path):
    step, messages = slice_with_intent(tmp_path / "rejected", [invalid, invalid])
    runtime = TaskReviewRuntime([step.executor.work_item])
    termination = await runtime.run({"w1": step}, deadline_seconds=10)
    assert termination == "model_response_rejected" and not runtime.failures
    report = project_task_runtime(runtime, {"w1": step}, list(step.executor.goals.values()), termination_reason=termination)
    assert report.execution_status == "failed" and not report.scope_complete
    assert report.initial_scope_coverage[0]["status"] == "not_examined"
    assert report.diagnostics[0]["reason_codes"] == ["unknown_author_target"]
    assert report.diagnostics[0]["format_corrections"] == 1
    assert "作者目标引用不在冻结声明中" in report.failure_summary
    assert "return value" not in report.failure_summary and "technical_failure" not in report.failure_summary
    assert len(messages) == report.sent_calls == 2 and report.settled_tokens == 30
    task = completed_task(**report.model_dump(mode="json"))
    task.status = "failed"
    assert PublicationPlanner().build(task).outcome.kind == "failed"


@pytest.mark.parametrize("max_calls", [0, 1])
async def test_correction_never_bypasses_budget(tmp_path, max_calls):
    step, messages = slice_with_intent(tmp_path / str(max_calls), [invalid], max_calls=max_calls)
    runtime = TaskReviewRuntime([step.executor.work_item])
    assert await runtime.run({"w1": step}, deadline_seconds=10) == "budget_exhausted"
    assert len(messages) == step.budget.calls_consumed == max_calls
    assert not step.executor.projection.examinations


async def test_expanded_schema_still_obeys_model_context_admission(tmp_path):
    step, messages = slice_with_intent(tmp_path / "small-context", [valid])
    step.provider._profile_override = ModelRequestProfile(context_window=1000)
    runtime = TaskReviewRuntime([step.executor.work_item])
    assert await runtime.run({"w1": step}, deadline_seconds=10) == "technical_failure"
    assert not messages and step.budget.calls_consumed == 0
    assert runtime.failures


async def test_correction_and_terminal_reason_survive_checkpoint_without_extra_send(tmp_path, persistence):
    repository = persistence[0]
    repository.create_task(ReviewTask(id="run", pr_url="https://github.com/o/r/pull/1", engine="task_review_v1"))
    async def configure(name, responses):
        step, messages = slice_with_intent(tmp_path / name, responses)
        step.budget = await ReviewRunBudget.load_or_create("run", max_tokens=100000, max_calls=5,
            store=ReviewRepositoryRunBudgetStore(repository), recover_running=False)
        runtime = TaskReviewRuntime([step.executor.work_item])
        await TaskRuntimeCheckpoint(runtime, {"w1": step}, repository).restore()
        return runtime, step, messages
    runtime, step, _ = await configure("first", [invalid])
    await runtime.advance_one({"w1": step})
    assert step.repair_errors and step.executor.work_item.format_corrections == 1
    runtime, step, messages = await configure("second", [invalid])
    assert step.rejected_items and step.executor.work_item.format_corrections == 1
    assert await runtime.run({"w1": step}, deadline_seconds=10) == "model_response_rejected"
    assert len(messages) == 1 and step.budget.settled_actual == 30
    runtime, step, messages = await configure("third", [])
    assert await runtime.run({"w1": step}, deadline_seconds=10) == "model_response_rejected"
    assert not messages and step.budget.calls_consumed == 2
    report = project_task_runtime(runtime, {"w1": step}, list(step.executor.goals.values()), termination_reason="model_response_rejected")
    assert "unknown_author_target" in report.diagnostics[0]["reason_codes"]


async def test_production_node_and_report_keep_response_rejection_reason(production_input):
    state, provider, calls = production_input
    state["pr_info"] = {"title": "保留返回值。", "body": ""}
    state["task_runtime_configuration"].update(prompt_version="task-review-prompt-v6", pr_intent_contract="pr-intent-assessment-v3")
    class SDK:
        async def ainvoke(self, messages):
            payload = json.loads(messages[1].content.splitlines()[0])
            calls.append(payload)
            response = {"kind": "submit", "examinations": [{"target_id": goal["goal_id"], "disposition": "examined",
                "evidence_ids": payload["active_evidence_ids"], "reason": "检查整段", "intent_assessments": [{
                    "author_quote": "model-secret-not-a-target", "actual_behavior": "局部行为", "alignment": "supported",
                    "evidence_ids": payload["active_evidence_ids"]}]} for goal in payload["goals"]]}
            return AIMessage(content=json.dumps(response), usage_metadata={"input_tokens":10,"output_tokens":5,"total_tokens":15})
    provider._build_chat_model = lambda *args: SDK()
    state["_provider"] = provider
    update = await task_review_runtime_node(state)
    assert update["status"] == "failed" and len(calls) == 2
    report = update["task_runtime_report"]
    assert report["termination_reason"] == "model_response_rejected"
    assert "model-secret" not in json.dumps(report["diagnostics"])
    exported = await report_node({**state, **update})
    assert exported["status"] == "failed" and exported["run_manifest"]
    assert "unknown_author_target" in exported["report_markdown"]


def test_untrusted_diagnostic_values_are_not_exported():
    assert "secret" not in runtime_failure_summary([{"category":"model_response_rejected", "reason_codes":["secret"]}])
    assert "model_response_rejected" in runtime_failure_summary([{"category":"model_response_rejected", "reason_codes":None}])
