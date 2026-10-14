from copy import deepcopy
import json

import pytest

from app.agents.providers import OpenAICompatibleProvider
from app.models.review import (
    AgentAction,
    ExecutionBudget,
    ReviewUnitResult,
    UnitDiffBatch,
    UnitReviewPlan,
    UnitReviewRecord,
    UnitReviewResponse,
)
from app.review.unit_completion import is_review_unit_complete
from app.services.review_input_context import build_working_memory
from app.services.review_unit_executor import ReviewUnitExecutor
from app.services.unit_coverage import (build_diff_manifest, new_coverage_ledger, active_evidence_set,
    update_coverage, coverage_gaps, hierarchy_payload, MAX_ACTIVE_HUNKS)
from app.services.unit_review_summary import build_record_input, validate_record
from test_large_unit_worksets import large_file, review_unit, runner_and_state, WorksetProvider
from test_unit_review_summary import _file


def fixture(files=None):
    files = files or [_file()]
    unit = review_unit().model_copy(update={"primary_files": [file.file_path for file in files]})
    manifest = build_diff_manifest(unit, files, "head-a", "base-a")
    return unit, files, manifest, new_coverage_ledger(manifest)


def checked_summary(files, manifest, active, *, plan=None, memory=None, questions=None, updates=None):
    record = build_record_input(files, [], plan, manifest.snapshot["head_sha"], manifest.snapshot["base_sha"])
    record["diff_manifest"] = manifest.model_dump(mode="json")
    record["working_memory"] = memory or {}
    summary = validate_record(UnitReviewRecord(change_summary="检查活动范围", target_checks=[{
        "target": target, "status": "checked", "evidence_ids": [item.evidence_id for item in active.hunks if item.evidence_id],
        "reason": "逐个核对当前活动 Hunk"} for target in record["targets"]],
        unresolved_questions=questions or [], question_updates=updates or []), record, set(file.file_path for file in files))
    assert summary.status == "reported", summary.reason
    return summary


def test_cross_file_batch_support_carries_bound_prior_diff_evidence():
    files = [_file(), _file().model_copy(update={"file_path": "other.py"})]
    unit = review_unit().model_copy(update={"primary_files": [item.file_path for item in files]})
    manifest = build_diff_manifest(unit, files, "head-a", "base-a")
    prior_active = active_evidence_set(manifest, files[:1], "batch-1")
    prior_summary = checked_summary(files[:1], manifest, prior_active)
    prior_result = ReviewUnitResult(
        review_unit_id="batch-1",
        status="completed",
        review_summary=prior_summary,
        diff_manifest=manifest,
        active_evidence_set=prior_active,
    )
    entries = [
        UnitDiffBatch(
            id="batch-1",
            parent_unit_id=unit.id,
            input_hash="one",
            version="unit-diff-worksets-v1",
            ranges=[],
            admission={},
            status="completed",
            result=prior_result,
        ),
        UnitDiffBatch(
            id="batch-2",
            parent_unit_id=unit.id,
            input_hash="two",
            version="unit-diff-worksets-v1",
            ranges=[],
            admission={},
        ),
    ]
    worksets = [
        (unit, files[:1], {}),
        (unit, files[1:], {}),
    ]

    support = ReviewUnitExecutor._batch_supporting_context(
        worksets, entries, 1, manifest, files[1:], "head-a", "base-a"
    )

    assert support
    assert support[0]["file"] == "a.py"
    assert support[0]["source"] == "batch_diff_support"
    assert support[0]["derived_from_evidence_id"] == prior_active.hunks[0].evidence_id


def test_same_file_batch_support_carries_prior_hunk_evidence_without_duplicate_current_hunk():
    files = [_file()]
    unit, _, manifest, _ = fixture(files)
    prior_files = files
    current_files = [files[0].model_copy(update={
        "hunks": [files[0].hunks[0].model_copy(update={"new_start": 20})],
    })]
    prior_active = active_evidence_set(manifest, prior_files, "batch-1")
    prior_summary = checked_summary(prior_files, manifest, prior_active)
    prior_result = ReviewUnitResult(
        review_unit_id="batch-1",
        status="completed",
        review_summary=prior_summary,
        diff_manifest=manifest,
        active_evidence_set=prior_active,
    )
    entries = [
        UnitDiffBatch(
            id="batch-1",
            parent_unit_id=unit.id,
            input_hash="one",
            version="unit-diff-worksets-v1",
            ranges=[],
            admission={},
            status="completed",
            result=prior_result,
        ),
        UnitDiffBatch(
            id="batch-2",
            parent_unit_id=unit.id,
            input_hash="two",
            version="unit-diff-worksets-v1",
            ranges=[],
            admission={},
        ),
    ]
    support = ReviewUnitExecutor._batch_supporting_context(
        [(unit, prior_files, {}), (unit, current_files, {})],
        entries,
        1,
        manifest,
        current_files,
        "head-a",
        "base-a",
    )

    assert support
    assert support[0]["file"] == "a.py"
    assert support[0]["derived_from_evidence_id"] == prior_active.hunks[0].evidence_id


def test_manifest_has_complete_inventory_without_any_body_and_paths_disambiguate_hunk_ids():
    files = [_file(), _file().model_copy(update={"file_path": "other.py"})]
    for file in files:
        file.hunks[0].hunk_id = "same-id"
    _, _, manifest, ledger = fixture(files)
    assert len(manifest.hunks) == 2 and len(ledger.diff_hunks) == 2
    assert manifest.hunks[0].id != manifest.hunks[1].id
    assert "return value" not in manifest.model_dump_json()
    assert coverage_gaps(manifest, ledger)


def test_hunk_coverage_requires_checked_references_and_no_range_gaps():
    _, files, manifest, ledger = fixture([large_file(count=10, width=5)])
    from app.services.unit_worksets import build_unit_worksets
    batches = build_unit_worksets(review_unit(), files, lambda unit, files, metadata: {
        "admitted": len(files[0].hunks[0].lines) <= 6, "reason": "active_hunk_set_exceeded"})
    first, selected, metadata = batches[0]
    active = active_evidence_set(manifest, selected, first.id, metadata["ranges"])
    ledger = update_coverage(ledger, manifest, active, checked_summary(selected, manifest, active))
    assert coverage_gaps(manifest, ledger) and not coverage_gaps(manifest, ledger, active)
    for child, selected, metadata in batches[1:]:
        active = active_evidence_set(manifest, selected, child.id, metadata["ranges"])
        ledger = update_coverage(ledger, manifest, active, checked_summary(selected, manifest, active))
    assert not coverage_gaps(manifest, ledger)
    corrupted = ledger.model_copy(deep=True)
    corrupted.diff_hunks[manifest.hunks[0].id].reviewed_ranges = [[0, 3], [5, 10]]
    assert coverage_gaps(manifest, corrupted)


def test_required_high_risk_hypothesis_and_question_cannot_disappear_by_omission():
    _, files, manifest, ledger = fixture()
    active = active_evidence_set(manifest, files, "batch")
    plan = UnitReviewPlan(change_summary="边界检查", review_objectives=["返回值"], initial_action=AgentAction(action="report_issue", reason="检查"),
        risk_hypotheses=[{"id": "risk", "category": "correctness", "priority": "high", "description": "空值边界",
            "affected_files": ["a.py"], "evidence_needed": ["调用方"], "completion_criteria": "核对调用方"}])
    summary = checked_summary(files, manifest, active, plan=plan, questions=[{
        "question": "跨批契约待确认", "affected_files": ["a.py"], "evidence_ids": [active.hunks[0].evidence_id]}])
    ledger = update_coverage(ledger, manifest, active, summary, plan)
    ledger = update_coverage(ledger, manifest, active, checked_summary(files, manifest, active), plan)
    assert "risk" in coverage_gaps(manifest, ledger)
    assert any(item.status == "pending" for item in ledger.questions.values())


def test_same_manifest_memory_preserves_questions_but_never_cites_inactive_code():
    _, files, manifest, _ = fixture([_file(), _file().model_copy(update={"file_path": "other.py"})])
    active = active_evidence_set(manifest, files[:1], "first")
    summary = checked_summary(files[:1], manifest, active, questions=[{
        "question": "跨文件契约", "affected_files": ["a.py"], "evidence_ids": [active.hunks[0].evidence_id]}])
    request = build_record_input(files[1:], [], None, "head-a", "base-a")
    request["diff_manifest"] = manifest.model_dump(mode="json")
    memory, restored = build_working_memory(summary, request, "parent")
    assert not memory["memory_invalidated"] and memory["unresolved_questions"]
    assert not restored and memory["missing_evidence_ids"] == [active.hunks[0].evidence_id]
    assert memory["target_checks"][0]["status"] == "unresolved"
    request["snapshot"]["head_sha"] = "different"
    assert build_working_memory(summary, request, "parent")[0]["memory_invalidated"]


def test_hierarchy_rejects_manifest_or_active_evidence_tampering():
    unit, files, manifest, ledger = fixture()
    active = active_evidence_set(manifest, files, unit.id)
    record = build_record_input(files, [], None, "head-a", "base-a")
    state = {"unit_metadata": unit.model_dump(), "diff_manifest": manifest.model_dump(),
        "coverage_ledger": ledger.model_dump(), "active_evidence_set": active.model_dump()}
    assert hierarchy_payload(state, record["evidence"])
    bad = deepcopy(state)
    bad["active_evidence_set"]["hunks"][0]["evidence_id"] = "invented"
    with pytest.raises(ValueError, match="active_evidence_mismatch"):
        hierarchy_payload(bad, record["evidence"])
    bad = deepcopy(state)
    bad["diff_manifest"]["hunks"] = []
    with pytest.raises(ValueError, match="binding_mismatch"):
        hierarchy_payload(bad, record["evidence"])


def test_swapping_valid_ids_between_hunks_cannot_forge_coverage():
    files = [_file(), _file().model_copy(update={"file_path": "other.py"})]
    unit, files, manifest, ledger = fixture(files)
    active = active_evidence_set(manifest, files, unit.id)
    record = build_record_input(files, [], None, "head-a", "base-a")
    active.hunks[0].manifest_id, active.hunks[1].manifest_id = active.hunks[1].manifest_id, active.hunks[0].manifest_id
    state = {"unit_metadata": unit.model_dump(), "diff_manifest": manifest.model_dump(),
        "coverage_ledger": ledger.model_dump(), "active_evidence_set": active.model_dump()}
    with pytest.raises(ValueError, match="active_evidence_mismatch"):
        hierarchy_payload(state, record["evidence"])


@pytest.mark.asyncio
async def test_corrupted_cached_active_range_is_rebuilt_instead_of_reused():
    runner, state = runner_and_state()
    first = await runner.execute_unit(review_unit(), state)
    assert is_review_unit_complete(first)
    corrupted = first.model_copy(deep=True)
    corrupted.diff_batches[0].result.active_evidence_set.hunks[0].end_offset -= 1
    state["review_unit_results"] = [corrupted.model_dump(mode="json")]
    calls = len(runner.provider.requests)
    restored = await runner.execute_unit(review_unit(), state)
    assert is_review_unit_complete(restored)
    assert len(runner.provider.requests) == calls + 1


@pytest.mark.asyncio
async def test_growing_ledger_can_reject_future_admission_without_losing_prior_coverage():
    runner, state = runner_and_state()
    original = runner._workset_admission
    def reject_future(unit, files, parent, budget, metadata=None):
        if any(item["reviewed_ranges"] for item in parent.get("coverage_ledger", {}).get("diff_hunks", {}).values()):
            return {"admitted": False, "reason": "model_input_limit_exceeded"}
        return original(unit, files, parent, budget, metadata)
    runner._workset_admission = reject_future
    result = await runner.execute_unit(review_unit(), state)
    assert result.diff_manifest and result.coverage_ledger
    assert any(batch.status == "completed" for batch in result.diff_batches)
    assert any(batch.status == "skipped" for batch in result.diff_batches)
    assert not is_review_unit_complete(result)
    assert result.error is None


class EarlyDoneProvider(WorksetProvider):
    async def decide(self, state, model):
        return AgentAction(action="task_done", reason="模型提前结束")


@pytest.mark.asyncio
async def test_task_done_before_diagnosis_goes_through_server_coverage_gate():
    runner, state = runner_and_state(EarlyDoneProvider())
    state["changed_files"] = [_file().model_dump(mode="json")]
    result = await runner.execute_unit(review_unit(), state)
    assert len(runner.provider.requests) == 1 and is_review_unit_complete(result)
    assert result.coverage_ledger and not coverage_gaps(result.diff_manifest, result.coverage_ledger)


@pytest.mark.asyncio
async def test_every_batch_sees_full_manifest_parent_metadata_and_only_active_bodies():
    runner, state = runner_and_state()
    result = await runner.execute_unit(review_unit(), state)
    assert is_review_unit_complete(result)
    assert result.diff_manifest.review_unit_id == review_unit().id
    assert len(result.diff_batches) > 1
    for files, request, _ in runner.provider.requests:
        assert request["diff_manifest"] == result.diff_manifest.model_dump(mode="json")
        assert request["unit_metadata"]["id"] == review_unit().id
        assert len(request["active_evidence_set"]["hunks"]) <= MAX_ACTIVE_HUNKS
        assert len(request["evidence"]) == sum(len(file.hunks) for file in files)
        assert "coverage_ledger" in request
    second = runner.provider.requests[1][1]
    assert second["working_memory"].get("target_checks")
    first_batch = result.diff_batches[0].result
    prompt_state = runner._unit_state({**state, "diff_manifest": result.diff_manifest.model_dump(),
        "coverage_ledger": first_batch.coverage_ledger.model_dump(), "unit_metadata": review_unit().model_dump(),
        "unit_diff_batch": {"ranges": result.diff_batches[0].ranges}},
        review_unit().model_copy(update={"id": first_batch.review_unit_id}),
        runner.planner.build_scope(review_unit(), None), runner.provider.requests[0][0], ExecutionBudget(), [])
    prompt_state["unit_agent"] = True
    for builder, marker in [(OpenAICompatibleProvider._build_unit_plan_prompt, "Bounded Unit input JSON:\n"),
                            (OpenAICompatibleProvider._build_decision_prompt, "Current state JSON:\n")]:
        payload = json.loads(builder(prompt_state).split(marker)[1])
        assert payload["diff_manifest"] == prompt_state["diff_manifest"]
        assert payload["active_evidence_set"] == prompt_state["active_evidence_set"]


@pytest.mark.asyncio
async def test_failed_or_budget_limited_batches_keep_all_required_hunks_pending():
    runner, state = runner_and_state(calls=2, attempts=2)
    result = await runner.execute_unit(review_unit(), state)
    assert result.diff_manifest and result.coverage_ledger
    assert coverage_gaps(result.diff_manifest, result.coverage_ledger)
    assert not is_review_unit_complete(result)
    assert len(result.coverage_ledger.diff_hunks) == len(result.diff_manifest.hunks)


class ResolveAcrossBatches(WorksetProvider):
    async def review_unit(self, pr, files, diff, model, record_input):
        response = await super().review_unit(pr, files, diff, model, record_input)
        evidence_id = record_input["evidence"][0]["id"]
        if len(self.requests) == 1:
            response.review_record = response.review_record.model_copy(update={"unresolved_questions":
                UnitReviewRecord(change_summary="等待另一批", unresolved_questions=[{
                    "question": "另一批是否遵守同一契约", "affected_files": ["a.py"],
                    "evidence_ids": [evidence_id]}]).unresolved_questions})
        else:
            previous = record_input["working_memory"]["unresolved_questions"][0]
            response.review_record = response.review_record.model_copy(update={"question_updates":
                UnitReviewRecord(change_summary="核对新证据", question_updates=[{
                    "question_id": previous["id"], "status": "resolved", "reason": "本批代码已核对契约",
                    "evidence_ids": [evidence_id]}]).question_updates})
        return UnitReviewResponse(review_record=response.review_record)


@pytest.mark.asyncio
async def test_question_resolution_crosses_batches_and_survives_resume_without_rebilling():
    runner, state = runner_and_state(ResolveAcrossBatches())
    file = _file()
    file.hunks = [file.hunks[0].model_copy(deep=True, update={"hunk_id": f"h{index}"}) for index in range(5)]
    state["changed_files"] = [file.model_dump(mode="json")]
    first = await runner.execute_unit(review_unit(), state)
    assert is_review_unit_complete(first), first.model_dump()
    assert len(first.diff_batches) == 2
    assert not first.review_summary.record.unresolved_questions
    assert all(item.status == "resolved" for item in first.coverage_ledger.questions.values())
    calls = len(runner.provider.requests)
    state["review_unit_results"] = [first.model_dump(mode="json")]
    second = await runner.execute_unit(review_unit(), state)
    assert is_review_unit_complete(second), second.model_dump()
    assert len(runner.provider.requests) == calls
    assert second.execution_budget == first.execution_budget


@pytest.mark.asyncio
async def test_300k_unit_uses_bounded_active_sets_and_keeps_full_manifest():
    from app.services.model_request_budgeter import ModelRequestProfile
    provider = WorksetProvider()
    provider._default_model = "gpt-4o"
    provider._request_profile = ModelRequestProfile(context_window=32000)
    runner, state = runner_and_state(provider, timeout=30)
    file = large_file(count=35, width=1070)
    file.hunks = [file.hunks[0].model_copy(deep=True, update={"hunk_id": f"original-{index}"}) for index in range(8)]
    for index, hunk in enumerate(file.hunks):
        hunk.new_start += index * 35
        for line in hunk.lines:
            line.new_line_no += index * 35
        for line in hunk.added_lines:
            line.line_no += index * 35
    file.additions = 280
    state["changed_files"] = [file.model_dump(mode="json")]
    unit = review_unit()
    assert len(runner._unit_diff(unit, {file.file_path: file})) > 300000
    result = await runner.execute_unit(unit, state)
    assert is_review_unit_complete(result), {"coverage": result.input_coverage.model_dump(),
        "batches": [(entry.status, entry.reason, entry.admission.get("reason")) for entry in result.diff_batches],
        "ledger_gaps": coverage_gaps(result.diff_manifest, result.coverage_ledger)}
    assert len(result.diff_manifest.hunks) == 8
    assert 1 < len(result.diff_batches) <= 32
    assert not coverage_gaps(result.diff_manifest, result.coverage_ledger)
    assert all(item[2]["reserved_tokens"] + 512 <= 32000 for item in provider.requests)
    assert result.execution_budget.model_calls <= 64
