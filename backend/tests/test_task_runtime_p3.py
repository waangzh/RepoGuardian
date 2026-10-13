import hashlib
import json
from copy import deepcopy

import pytest

from app.models.review import ReviewTask
from app.models.review_runtime import ReviewGoal, ReviewWorkItem
from app.services.review_run_budget import ReviewRepositoryRunBudgetStore, ReviewRunBudget
from app.services.review_runtime import TaskReviewRuntime
from app.services.review_task_derivation import TaskReviewDerivation, build_task_risk_catalog
from app.services.task_runtime_checkpoint import TaskRuntimeCheckpoint
from test_phase6a_persistence import persistence as persistence
from test_task_review_sdk_slice import make_slice


def candidate(*, claim="空返回值破坏调用方契约", scenario="服务返回空值时调用方直接读取字段", requests=None):
    return {"claim": claim, "primary_anchor": "t1", "evidence_ids": ["e1"],
            "affected_behavior": "调用方字段读取失败", "failure_scenario": scenario,
            "recommendation": "在读取返回值前处理空值", "evidence_requests": requests or []}


def inspect_submit(candidates=(), leads=()):
    return {"kind": "submit", "examinations": [{"target_id": "t1", "disposition": "examined",
        "evidence_ids": ["e1"], "reason": "核对固定源码"}], "candidates": list(candidates), "leads": list(leads)}


def verification(payload, decision="supported"):
    assert payload["task"]["kind"] == "verify"
    assert "confidence" not in payload["candidate"]
    assert not {"question_updates", "obligation_updates", "review_record"} & set(payload)
    result = {"kind": "submit", "verification": {"candidate_id": payload["candidate"]["candidate_id"],
        "decision": decision, "evidence_ids": payload["active_evidence_ids"], "reason": "独立核对主张及必要反证"}}
    if payload.get("publication_contract") == "claim-publication-v1":
        result["verification"].update(subject_hash=payload["verification_subject_hash"],
            publication_action="retain" if decision == "supported" else "none")
    return result


def investigation(payload, answer="refuted", candidates=(), leads=()):
    assert payload["task"]["kind"] == "investigate" and payload["proposition"]
    return {"kind": "submit", "investigation": {"answer": answer,
        "evidence_ids": payload["active_evidence_ids"], "limitations": [], "reason": "核对具体关系"},
        "candidates": list(candidates), "leads": list(leads)}


def configured(tmp_path, responses, **kwargs):
    step, messages = make_slice(tmp_path, responses)
    step.executor.work_item.unit_id = "u1"
    runtime = TaskReviewRuntime([step.executor.work_item])
    steps = {"w1": step}
    derivation = TaskReviewDerivation(runtime, steps, **kwargs)
    return runtime, steps, derivation, messages


async def test_identical_candidates_share_one_independent_verification_preserving_sources(tmp_path):
    runtime, steps, derivation, messages = configured(tmp_path, [inspect_submit([candidate(), candidate()]), verification])
    assert await runtime.run(steps, deadline_seconds=5) == "plan_exhausted"
    assert len(messages) == 2 and len(steps) == 2
    record = next(iter(derivation.data["candidates"].values()))
    assert record["status"] == "confirmed"
    assert len(record["source_candidate_ids"]) == 2
    assert len(derivation.data["verification_cache"]) == 1
    budget = steps["w1"].budget
    assert budget.calls_consumed == 2 and budget.settled_actual == 30
    assert {attempt.operation for attempt in budget.attempts.values()} == {"task_review_v1"}
    assert {json.loads(message[1].content.split("\n")[0])["task"]["kind"] for message in messages} == {"inspect", "verify"}


async def test_different_failure_at_same_line_is_never_merged(tmp_path):
    runtime, steps, derivation, messages = configured(tmp_path, [inspect_submit([
        candidate(), candidate(claim="旧调用方不兼容新字段", scenario="新字段重命名后旧调用方仍读取旧字段")]),
        verification, verification])
    assert await runtime.run(steps, deadline_seconds=5) == "plan_exhausted"
    assert len(messages) == 3 and len(derivation.data["candidates"]) == 2
    assert all(record["status"] == "confirmed" for record in derivation.data["candidates"].values())


async def test_missing_specific_failure_stays_visible_without_verifier_send(tmp_path):
    value = candidate()
    value["failure_scenario"] = ""
    runtime, steps, derivation, messages = configured(tmp_path, [inspect_submit([value])])
    assert await runtime.run(steps, deadline_seconds=5) == "plan_exhausted"
    record = next(iter(derivation.data["candidates"].values()))
    assert record["status"] == "unresolved" and "failure" in record["reason"]
    assert len(messages) == 1


async def test_insufficient_evidence_is_not_confirmed_or_cached(tmp_path):
    runtime, steps, derivation, messages = configured(tmp_path, [inspect_submit([candidate()]),
        lambda payload: verification(payload, "insufficient_evidence")])
    assert await runtime.run(steps, deadline_seconds=5) == "plan_exhausted"
    record = next(iter(derivation.data["candidates"].values()))
    assert record["status"] == "unresolved" and not derivation.data["verification_cache"]
    assert len(messages) == 2


async def test_refuted_relation_finishes_investigation_and_can_reveal_a_candidate(tmp_path):
    lead = {"proposition": "返回值变化不会破坏调用方", "related_target_ids": ["t1"],
        "missing_evidence": ["明确的调用契约"], "evidence_requests": [{
            "file_path": "src/a.py", "start_line": 1, "end_line": 1}]}
    runtime, steps, derivation, messages = configured(tmp_path, [inspect_submit(leads=[lead]),
        lambda payload: investigation(payload, candidates=[candidate()]), verification])
    assert await runtime.run(steps, deadline_seconds=5) == "plan_exhausted"
    investigation_step = next(step for step in steps.values() if step.executor.work_item.kind.value == "investigate")
    assert investigation_step.executor.projection.investigation.answer.value == "refuted"
    assert investigation_step.executor.work_item.state.value == "completed"
    assert steps["w1"].executor.projection.examinations["t1"].disposition.value == "examined"
    assert next(iter(derivation.data["candidates"].values()))["status"] == "confirmed"
    assert len(messages) == 3


async def test_second_generation_lead_retained_but_not_executed(tmp_path):
    lead = {"proposition": "明确关系命题", "related_target_ids": ["t1"], "missing_evidence": ["契约"],
            "evidence_requests": [{"file_path": "src/a.py", "start_line": 1, "end_line": 1}]}
    child_lead = {**lead, "proposition": "还需要进一步调查另一个条件"}
    runtime, steps, derivation, messages = configured(tmp_path, [inspect_submit(leads=[lead]),
        lambda payload: investigation(payload, leads=[child_lead])])
    assert await runtime.run(steps, deadline_seconds=5) == "plan_exhausted"
    unscheduled = [lead for lead in derivation.data["leads"].values() if lead["status"] == "not_scheduled"]
    assert len(unscheduled) == 1 and unscheduled[0]["reason"] == "second_generation_disabled"
    assert len(messages) == 2 and len(steps) == 2


async def test_evidence_preparation_happens_before_verification(tmp_path):
    requests = [{"file_path": "src/a.py", "start_line": 2, "end_line": 2}]
    runtime, steps, derivation, messages = configured(tmp_path, [inspect_submit([candidate(requests=requests)]),
        lambda payload: investigation(payload), verification])
    (tmp_path / "src/a.py").write_text("return value\ncaller(value)\n", encoding="utf-8")
    steps["w1"].git.get_file_content_at_revision = lambda *args: "return value\ncaller(value)\n"
    assert await runtime.run(steps, deadline_seconds=5) == "plan_exhausted"
    assert [json.loads(message[1].content.split("\n")[0])["task"]["kind"] for message in messages] == ["inspect", "investigate", "verify"]
    verify_prompt = json.loads(messages[-1][1].content.split("\n")[0])
    assert any(evidence["content"] == "caller(value)" for evidence in verify_prompt["active_evidence"])
    assert next(iter(derivation.data["candidates"].values()))["status"] == "confirmed"


async def test_out_of_scope_evidence_request_no_verifier_no_scope_expansion(tmp_path):
    requests = [{"file_path": "private/token.py", "start_line": 1, "end_line": 2}]
    runtime, steps, derivation, messages = configured(tmp_path, [inspect_submit([candidate(requests=requests)])])
    assert await runtime.run(steps, deadline_seconds=5) == "plan_exhausted"
    record = next(iter(derivation.data["candidates"].values()))
    assert record["status"] == "unresolved" and "scope" in record["reason"]
    assert len(messages) == 1 and len(steps) == 1


async def test_known_counterevidence_required_and_wrong_candidate_rejected(tmp_path):
    runtime, steps, derivation, _ = configured(tmp_path, [inspect_submit([candidate()])],
                                               counter_evidence_ids={"t1": ["counter"]})
    evidence = deepcopy(steps["w1"].executor.evidence_catalog["e1"])
    evidence.update(content="guard(value)", start_line=2, end_line=2,
                    content_hash=hashlib.sha256(b"guard(value)").hexdigest())
    steps["w1"].executor.evidence_catalog["counter"] = evidence
    await runtime.advance_one(steps)
    step = next(step for key, step in steps.items() if key != "w1")
    executor = step.executor
    correct_id = executor.work_item.verification_candidate.candidate_id
    def output(candidate_id, ids):
        return {"kind": "submit", "verification": {"candidate_id": candidate_id, "decision": "supported",
                "evidence_ids": ids, "reason": "判定"}}
    parsed = executor.parse_and_apply(output("wrong", ["e1", "counter"]))
    assert parsed.errors[0].reason == "verification_candidate_identity_mismatch"
    parsed = executor.parse_and_apply(output(correct_id, ["e1"]))
    assert parsed.errors[0].reason == "incomplete_verification_evidence"
    executor.active_evidence_ids.remove("counter")
    parsed = executor.parse_and_apply(output(correct_id, ["e1", "counter"]))
    assert parsed.errors[0].reason == "unknown_or_inactive_evidence"
    assert executor.projection.verification is None


async def test_investigation_invalid_answer_preserves_valid_candidate_sibling(tmp_path):
    lead = {"proposition": "明确命题", "related_target_ids": ["t1"], "missing_evidence": ["契约"],
            "evidence_requests": [{"file_path": "src/a.py", "start_line": 1, "end_line": 1}]}
    runtime, steps, _, _ = configured(tmp_path, [inspect_submit(leads=[lead])])
    await runtime.advance_one(steps)
    executor = next(step.executor for key, step in steps.items() if key != "w1")
    parsed = executor.parse_and_apply({"kind": "submit", "investigation": {"answer": "unknown_enum"},
                                       "candidates": [candidate()], "leads": []})
    assert parsed.errors and executor.projection.candidates
    assert executor.projection.investigation is None and executor.projection.pending_target_ids


def test_deterministic_risk_catalog_has_real_binding_and_no_synthetic_unit():
    goals = [ReviewGoal(goal_id="t1", unit_id="u1", kind="inspect", target_ref="src/a.py"),
             ReviewGoal(goal_id="t2", unit_id="u2", kind="inspect", target_ref="src/b.py")]
    work = [ReviewWorkItem(work_item_id="w1", kind="inspect", goal_ids=["t1"]),
            ReviewWorkItem(work_item_id="w2", kind="inspect", goal_ids=["t2"])]
    catalog = build_task_risk_catalog(goals, work, {"edges": [{"type": "calls",
        "source": "src/a.py::caller", "target": "src/b.py::callee"}]})
    assert len(catalog) == 1 and catalog[0]["related_goal_ids"] == ["t1", "t2"]
    assert "calls" in catalog[0]["proposition"] and len(catalog[0]["evidence_requests"]) == 2


async def test_derived_queue_and_verification_results_survive_database_restart(tmp_path, persistence):
    repository = persistence[0]
    repository.create_task(ReviewTask(id="run", pr_url="https://github.com/a/b/pull/1", engine="task_review_v1"))
    first_root = tmp_path / "first"
    first_root.mkdir()
    runtime, steps, derivation, _ = configured(first_root, [inspect_submit([candidate()]), verification])
    budget = await ReviewRunBudget.load_or_create("run", max_tokens=100_000, max_calls=5,
        store=ReviewRepositoryRunBudgetStore(repository), recover_running=False)
    steps["w1"].budget = budget
    checkpoint = TaskRuntimeCheckpoint(runtime, steps, repository)
    await checkpoint.restore()
    await runtime.advance_one(steps)
    assert len(steps) == 2
    second_root = tmp_path / "second"
    second_root.mkdir()
    runtime2, steps2, derivation2, messages2 = configured(second_root, [verification])
    steps2["w1"].budget = await ReviewRunBudget.load_or_create("run", max_tokens=0, max_calls=0,
        store=ReviewRepositoryRunBudgetStore(repository), recover_running=False)
    checkpoint2 = TaskRuntimeCheckpoint(runtime2, steps2, repository)
    await checkpoint2.restore()
    assert len(steps2) == 2 and len(derivation2.data["candidates"]) == 1
    assert await runtime2.run(steps2, deadline_seconds=5) == "plan_exhausted"
    assert len(messages2) == 1 and steps2["w1"].budget.settled_actual == 30
    assert next(iter(derivation2.data["candidates"].values()))["status"] == "confirmed"
    third_root = tmp_path / "third"
    third_root.mkdir()
    runtime3, steps3, derivation3, messages3 = configured(third_root, [])
    steps3["w1"].budget = await ReviewRunBudget.load_or_create("run", max_tokens=0, max_calls=0,
        store=ReviewRepositoryRunBudgetStore(repository), recover_running=False)
    await TaskRuntimeCheckpoint(runtime3, steps3, repository).restore()
    assert await runtime3.run(steps3, deadline_seconds=5) == "plan_exhausted"
    assert not messages3 and steps3["w1"].budget.calls_consumed == 2
    assert next(iter(derivation3.data["candidates"].values()))["status"] == "confirmed"


async def test_policy_rejects_unruled_style_without_paid_verification(tmp_path):
    value = candidate(claim="代码风格命名偏好")
    value["category"] = "maintainability"
    runtime, steps, derivation, messages = configured(tmp_path, [inspect_submit([value])])
    assert await runtime.run(steps, deadline_seconds=5) == "plan_exhausted"
    assert "pure_style_preference_without_rule" in next(iter(derivation.data["candidates"].values()))["reason"]
    assert len(messages) == 1


@pytest.mark.parametrize("tool,args", [
    ("file_find", {"query": "a.py", "max_results": 2}),
    ("code_search", {"query": "return", "relation": "text", "max_results": 2}),
    ("file_read_diff", {"file_path": "src/a.py"}),
])
async def test_readonly_tools_return_bounded_observations_and_submit_next(tmp_path, monkeypatch, tool, args):
    read = {"kind": "read", "tool": tool, "arguments": args, "purpose": "核对当前源码"}
    def finish(payload):
        return inspect_submit()
    runtime, steps, _, messages = configured(tmp_path, [read, finish])
    step = steps["w1"]
    monkeypatch.setattr("app.tools.context_files.list_git_tracked_files", lambda *args: {"src/a.py"})
    step.changed_files = [{"file_path": "src/a.py", "change_type": "modified", "hunks": [],
                           "additions": 1, "deletions": 0}]
    class Search:
        async def retrieve_context(self, **kwargs):
            assert kwargs["scope"].readable_files == {"src/a.py"}
            return [{"file": "src/a.py", "start_line": 1, "end_line": 1, "content": "return value",
                     "source": "code_search", "truncated": False},
                    {"file": "outside.py", "start_line": 1, "end_line": 1, "content": "secret"}]
    step.search_tool = Search()
    await runtime.advance_one(steps)
    observed = await runtime.advance_one(steps)
    assert observed["status"] == "completed" and observed["tool"] == tool
    if tool == "code_search":
        assert observed["evidence_ids"] and observed["rejected_chunks"] == 1
        assert not any(value["file_path"] == "outside.py" for value in step.executor.evidence_catalog.values())
    await runtime.advance_one(steps)
    assert "recent_tool_observations" in messages[-1][1].content
    assert len(messages) == 2 and step.budget.calls_consumed == 2


async def test_tool_discriminator_and_shell_action_rejected(tmp_path):
    runtime, steps, _, _ = configured(tmp_path, [])
    executor = steps["w1"].executor
    for raw in [
        {"kind": "read", "tool": "file_find", "arguments": {"file_path": "src/a.py"}, "purpose": "读取"},
        {"kind": "read", "tool": "shell", "arguments": {"command": "test"}, "purpose": "运行"},
    ]:
        assert executor.parse_and_apply(raw).response is None
    assert runtime.ready()


async def test_unknown_verifier_usage_remains_unresolved_and_never_cached(tmp_path):
    runtime, steps, derivation, messages = configured(tmp_path, [inspect_submit([candidate()]), verification])
    step = steps["w1"]
    await runtime.advance_one(steps)
    original = step.provider._extract_model_usage
    def unknown(*args, **kwargs):
        return original(*args, **kwargs).model_copy(update={"usage_available": False,
            "actual_total_tokens": None, "actual_input_tokens": None, "actual_output_tokens": None})
    step.provider._extract_model_usage = unknown
    await runtime.advance_one(steps)
    record = next(iter(derivation.data["candidates"].values()))
    assert record["status"] == "unresolved" and record["reason"] == "verification_usage_unknown"
    assert not derivation.data["verification_cache"] and len(messages) == 2
    assert step.budget.unknown_reserved > 0


async def test_changed_evidence_invalidates_cache_and_requires_new_verification(tmp_path):
    runtime, steps, derivation, messages = configured(tmp_path, [inspect_submit([candidate()]), verification, verification])
    assert await runtime.run(steps, deadline_seconds=5) == "plan_exhausted"
    key, record = next(iter(derivation.data["candidates"].items()))
    old_key = record["verification_key"]
    derivation.invalidate_evidence(["e1"], reason="new_counterevidence")
    assert record["status"] == "unresolved" and not derivation.data["verification_cache"]
    for step in steps.values():
        step.executor.evidence_catalog["e1"].update(content="return changed_value",
            content_hash=hashlib.sha256(b"return changed_value").hexdigest())
        step.executor.active_evidence_ids.add("e1")
    derivation.revalidate_candidate(key)
    assert record["verification_key"] != old_key
    await runtime.advance_one(steps)
    assert len(messages) == 3 and steps["w1"].budget.calls_consumed == 3
    assert record["status"] == "confirmed"


async def test_answered_investigation_only_requeues_bound_blocked_scope(tmp_path):
    lead = {"proposition": "明确契约是否成立", "related_target_ids": ["t1"], "missing_evidence": ["契约"],
            "evidence_requests": [{"file_path": "src/a.py", "start_line": 1, "end_line": 1}]}
    blocked = inspect_submit(leads=[lead])
    blocked["examinations"][0]["disposition"] = "blocked"
    runtime, steps, _, messages = configured(tmp_path, [blocked, lambda payload: investigation(payload), inspect_submit()])
    await runtime.advance_one(steps)
    assert steps["w1"].executor.projection.examinations["t1"].disposition.value == "blocked"
    await runtime.advance_one(steps)
    assert steps["w1"].executor.projection.pending_target_ids == {"t1"}
    assert "t1" not in steps["w1"].executor.projection.examinations
    await runtime.advance_one(steps)
    assert steps["w1"].executor.projection.examinations["t1"].disposition.value == "examined"
    assert len(messages) == 3
