import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.review.runtime_spec import task_runtime_configuration
from app.models.review import ReviewTask
from app.models.review_runtime import ReviewGoal, ReviewRunSpec, ReviewWorkItem
from app.core.fingerprints import stable_hash
from app.services.review.review_run_budget import ReviewRepositoryRunBudgetStore, ReviewRunBudget
from app.services.review.runtime.review_runtime import TaskReviewRuntime
from app.services.review.runtime.review_task_executor import ReviewTaskExecutor
from app.services.review.runtime.review_task_protocol import TaskReviewV1Protocol
from app.services.review.task_runtime_checkpoint import TaskRuntimeCheckpoint
from app.services.review.task_review_projection import project_task_runtime
from tests.review.test_intent_context_completeness import spec_for
from tests.review.test_intent_response_recovery import slice_with_intent
from tests.support.persistence import persistence as persistence


FIXTURES = Path(__file__).parents[1] / "fixtures"
NEW_CONTRACT = "structured-intent-v5"


def overlap_slice(folder, responses=None, *, contract=NEW_CONTRACT, side="head"):
    step, messages = slice_with_intent(folder, responses if responses is not None else [], contract=contract)
    e = step.executor
    e.goals["t1"] = e.goals["t1"].model_copy(update={
        "start_line": 437, "end_line": 459,
        "target_kind": "base_source_range" if side == "base" else "source_range"})
    template = deepcopy(e.evidence_catalog["e1"])
    e.evidence_catalog = {}
    for identity, start, end in (("before", 380, 455), ("exact", 437, 459), ("after", 456, 470)):
        content = "\n".join(f"value_{index} = {index}" for index in range(start, end + 1))
        e.evidence_catalog[identity] = {
            **template, "start_line": start, "end_line": end, "side": side,
            "content": content, "content_hash": hashlib.sha256(content.encode()).hexdigest()}
    e.active_evidence_ids = set(e.evidence_catalog)
    step.protocol = TaskReviewV1Protocol(e.spec)
    return step, messages


def submit(payload):
    return {"kind": "submit", "examinations": [{
        "target_id": "t1", "disposition": "examined", "reason": "核对目标及前后上下文。",
        "evidence_ids": ["before", "exact", "after"], "intent_assessments": [{
            "author_quote": payload["author_targets"][0], "actual_behavior": "保留局部返回值处理。",
            "author_goal_summary": "保留返回值。", "alignment": "supported",
            "evidence_ids": ["before", "after"], "limitations": ["仅局部静态观察"]}]}]}


def request_for(step):
    e = step.executor
    return step.protocol.prepare(e.work_item, e.current_goals(),
        active_evidence_ids=e.active_evidence_ids, evidence_catalog=e.evidence_catalog)


@pytest.mark.parametrize("contract,expected", [
    ("structured-intent-v2", "intent_evidence_scope_mismatch"),
    ("structured-intent-v3", "intent_evidence_scope_mismatch"),
    ("structured-intent-v4", "intent_evidence_scope_mismatch"),
    (NEW_CONTRACT, None),
])
@pytest.mark.parametrize("side", ["head", "base"])
def test_overlapping_context_is_versioned_and_side_aware(tmp_path, contract, expected, side):
    step, _ = overlap_slice(tmp_path / "slice", contract=contract, side=side)
    value = submit(json.loads(request_for(step).prompt))
    if contract not in {"structured-intent-v4", NEW_CONTRACT}:
        value["examinations"][0]["intent_assessments"][0].pop("author_goal_summary")
    parsed = step.executor.parse_and_apply(value)
    assert [error.reason for error in parsed.errors] == ([expected] if expected else [])
    assert bool(step.executor.projection.examinations) is (expected is None)


@pytest.mark.parametrize("case,reason", [
    ("before_disjoint", "intent_evidence_scope_mismatch"),
    ("after_disjoint", "intent_evidence_scope_mismatch"),
    ("file", "intent_evidence_scope_mismatch"),
    ("side", "intent_evidence_scope_mismatch"),
    ("stale", "stale_evidence_snapshot"),
    ("inactive", "unknown_or_inactive_evidence"),
    ("hash", "evidence_content_hash_mismatch"),
    ("range", "invalid_evidence_source_range"),
    ("foreign", "intent_evidence_outside_examination"),
    ("coverage", "incomplete_goal_evidence"),
    ("quote", "unknown_author_target"),
])
def test_overlap_never_bypasses_evidence_or_full_coverage_checks(tmp_path, case, reason):
    step, _ = overlap_slice(tmp_path / "slice")
    e = step.executor
    value = submit(json.loads(request_for(step).prompt))
    item = value["examinations"][0]
    assessment = item["intent_assessments"][0]
    evidence = e.evidence_catalog["before"]
    if case in {"before_disjoint", "after_disjoint"}:
        start, end = (400, 436) if case == "before_disjoint" else (460, 480)
        content = "\n".join(f"value_{index} = {index}" for index in range(start, end + 1))
        evidence.update(start_line=start, end_line=end, content=content,
                        content_hash=hashlib.sha256(content.encode()).hexdigest())
    elif case == "file":
        evidence["file_path"] = "src/b.py"
        e.work_item.scope_files.append("src/b.py")
    elif case == "side":
        evidence["side"] = "base"
        e.goals["base"] = ReviewGoal(goal_id="base", unit_id="u1", kind="inspect",
            target_ref="src/a.py", target_kind="base_source_range")
    elif case == "stale":
        evidence["head_sha"] = "c" * 40
    elif case == "inactive":
        e.active_evidence_ids.remove("before")
    elif case == "hash":
        evidence["content"] = "tampered"
    elif case == "range":
        evidence["end_line"] += 1
    elif case == "foreign":
        item["evidence_ids"] = ["exact"]
    elif case == "coverage":
        item["evidence_ids"] = ["before"]
        assessment["evidence_ids"] = ["before"]
    elif case == "quote":
        assessment["author_quote"] = "untrusted invented target"
    result = e.parse_and_apply(value)
    assert [error.reason for error in result.errors] == [reason]
    assert not e.projection.examinations


@pytest.mark.parametrize("start,end", [(437, 437), (459, 459), (380, 470), (437, 459)])
def test_inclusive_overlap_accepts_boundary_lines_and_superset_chunks(tmp_path, start, end):
    step, _ = overlap_slice(tmp_path / "slice")
    e = step.executor
    value = submit(json.loads(request_for(step).prompt))
    evidence = e.evidence_catalog["before"]
    content = "\n".join(f"line_{index}" for index in range(start, end + 1))
    evidence.update(start_line=start, end_line=end, content=content,
                    content_hash=hashlib.sha256(content.encode()).hexdigest())
    value["examinations"][0]["intent_assessments"][0]["evidence_ids"] = ["before"]
    assert not e.parse_and_apply(value).errors


def test_overlapping_context_pair_can_cover_goal_without_exact_chunk(tmp_path):
    step, _ = overlap_slice(tmp_path / "slice")
    value = submit(json.loads(request_for(step).prompt))
    value["examinations"][0]["evidence_ids"] = ["before", "after"]
    result = step.executor.parse_and_apply(value)
    assert not result.errors
    assert step.executor.projection.examinations["t1"].evidence_ids == ["before", "after"]
    assert (step.executor.goals["t1"].start_line, step.executor.goals["t1"].end_line) == (437, 459)


@pytest.mark.parametrize("response_index", [0, 1])
def test_real_saved_responses_accept_overlap_only_under_new_contract(response_index):
    saved = json.loads((FIXTURES / "intent_scope_overlap_failure.json").read_text(encoding="utf-8"))
    for contract in ("structured-intent-v4", NEW_CONTRACT):
        spec = ReviewRunSpec.model_validate({**saved["spec"], "intent_assessment_contract": contract})
        e = ReviewTaskExecutor(spec, ReviewWorkItem.model_validate(saved["work_item"]),
            [ReviewGoal.model_validate(goal) for goal in saved["goals"]],
            active_evidence_ids=set(saved["active"]), evidence_catalog=deepcopy(saved["evidence"]),
            request_id="offline-replay")
        before = deepcopy(e.evidence_catalog)
        first = e.parse_and_apply(saved["responses"][response_index])
        if contract == NEW_CONTRACT:
            assert not first.errors and len(e.projection.accepted_target_ids) == (2 if response_index == 0 else 1)
        else:
            assert [error.reason for error in first.errors] == ["intent_evidence_scope_mismatch"]
            assert len(e.projection.accepted_target_ids) == (1 if response_index == 0 else 0)
        assert e.evidence_catalog == before


def test_legacy_prompt_and_spec_identities_are_byte_stable(tmp_path):
    saved = json.loads((FIXTURES / "intent_scope_legacy_prompt_hashes.json").read_text(encoding="utf-8"))
    for key, expected in saved.items():
        prompt, contract = key.split(":")
        step, _ = slice_with_intent(tmp_path / key.replace(":", "-"), [], contract=contract)
        e = step.executor
        e.spec = e.spec.model_copy(update={"prompt_version": prompt})
        step.protocol.spec = e.spec
        request = request_for(step)
        assert stable_hash({"system": request.system, "prompt": request.prompt,
            "spec": e.spec.model_dump(mode="json")}) == expected


def test_new_default_mapping_and_prompt_are_consistent():
    configuration = task_runtime_configuration()
    assert configuration["pr_intent_contract"] == "pr-intent-assessment-v6"
    spec = spec_for("文" * 4001, configuration["pr_intent_contract"])
    assert spec.intent_assessment_contract == NEW_CONTRACT
    assert spec.pr_intent_context_truncated and len(spec.pr_intent_context) == 4000
    assert ReviewRunSpec.model_validate_json(spec.model_dump_json()) == spec
    assert spec_for("", configuration["pr_intent_contract"]).intent_assessment_contract is None


async def test_correction_explains_overlap_and_retains_single_shared_budget(tmp_path):
    def invalid_quote(payload):
        value = submit(payload)
        value["examinations"][0]["intent_assessments"][0]["author_quote"] = "invented author target"
        return value

    step, messages = overlap_slice(tmp_path / "slice", [invalid_quote, submit])
    runtime = TaskReviewRuntime([step.executor.work_item])
    assert await runtime.run({"w1": step}, deadline_seconds=10) == "plan_exhausted"
    feedback = json.loads(messages[1][1].content.splitlines()[-1])
    assert "overlap" in feedback["intent_correction"]
    assert "extending beyond its boundaries is allowed" in feedback["intent_correction"]
    assert "each cited evidence chunk to overlap" in messages[0][0].content
    assert "inside the goal range" not in messages[0][0].content
    assert step.executor.work_item.format_corrections == 1
    assert step.budget.calls_consumed == len(messages) == 2
    assert step.budget.settled_actual == 30


@pytest.mark.parametrize("max_calls", [0, 1])
async def test_overlap_contract_does_not_add_correction_budget(tmp_path, max_calls):
    def invalid_quote(payload):
        value = submit(payload)
        value["examinations"][0]["intent_assessments"][0]["author_quote"] = "invented author target"
        return value

    step, messages = overlap_slice(tmp_path / "slice", [invalid_quote, submit])
    step.budget.max_calls = max_calls
    step.executor.spec = step.executor.spec.model_copy(update={"max_calls": max_calls})
    step.protocol.spec = step.executor.spec
    runtime = TaskReviewRuntime([step.executor.work_item])
    assert await runtime.run({"w1": step}, deadline_seconds=10) == "budget_exhausted"
    assert step.budget.calls_consumed == len(messages) == max_calls
    assert not step.executor.projection.examinations


async def test_new_contract_survives_checkpoint_and_read_does_not_resend(tmp_path, persistence):
    repo = persistence[0]
    repo.create_task(ReviewTask(id="run", pr_url="https://github.com/o/r/pull/1", engine="task_review_v1"))

    async def configure(folder, responses):
        step, messages = overlap_slice(tmp_path / folder, responses)
        step.budget = await ReviewRunBudget.load_or_create("run", max_tokens=100000, max_calls=5,
            store=ReviewRepositoryRunBudgetStore(repo), recover_running=False)
        runtime = TaskReviewRuntime([step.executor.work_item])
        await TaskRuntimeCheckpoint(runtime, {"w1": step}, repo).restore()
        return step, messages, runtime

    step, messages, runtime = await configure("first", [submit])
    assert await runtime.run({"w1": step}, deadline_seconds=10) == "plan_exhausted"
    assert len(messages) == 1 and step.executor.work_item.format_corrections == 0
    accepted = step.executor.projection.examinations["t1"].model_dump(mode="json")
    restored, messages, runtime = await configure("restored", [])
    assert restored.executor.spec.intent_assessment_contract == NEW_CONTRACT
    assert restored.executor.projection.examinations["t1"].model_dump(mode="json") == accepted
    assert await runtime.run({"w1": restored}, deadline_seconds=10) == "plan_exhausted"
    assert messages == [] and restored.budget.calls_consumed == 1
    report = project_task_runtime(runtime, {"w1": restored}, list(restored.executor.goals.values()),
        termination_reason="plan_exhausted")
    assert report.scope_complete and report.initial_scope_coverage[0]["status"] == "examined"


def test_overlap_with_page_external_context_still_uses_current_page(tmp_path):
    from types import SimpleNamespace

    step, _ = overlap_slice(tmp_path / "slice")
    e = step.executor
    e.pages = [SimpleNamespace(page_id="p1", ranges=[{
        "goal_id": "t1", "start_line": 437, "end_line": 445}])]
    value = submit(json.loads(request_for(step).prompt))
    # The after chunk overlaps the full goal, but not the page being examined.
    assert [error.reason for error in e.parse_and_apply(value).errors] == ["intent_evidence_scope_mismatch"]
