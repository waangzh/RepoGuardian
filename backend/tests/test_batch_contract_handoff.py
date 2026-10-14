from app.models.review import (
    ContextSnippet,
    ExecutionBudget,
    PullRequestInfo,
    ReviewUnit,
    ReviewUnitComplexity,
    ReviewUnitResult,
    UnitDiffBatch,
    UnitContractDependency,
    UnitReviewRecord,
    UnitReviewSummary,
)
from app.agents.providers import OpenAICompatibleProvider
from app.services.review_input_context import build_context_evidence, make_evidence
from app.services.review_unit_executor import ReviewUnitExecutor
from app.services.unit_coverage import (
    active_evidence_set,
    build_diff_manifest,
    contract_dependency_key,
    rebuild_coverage,
)
from app.services.unit_review_summary import build_record_input, validate_record
from app.services.unit_coverage import contract_dependency_scope
from test_large_unit_worksets import WorksetProvider, large_file, review_unit
from test_unit_review_summary import _file, _state


def test_derived_prior_diff_evidence_can_verify_cross_batch_dependency() -> None:
    files = [_file()]
    original = make_evidence(
        "a.py", "diff", 1, 1, "prior implementation", "head", "base"
    )
    context = ContextSnippet(
        file="a.py",
        start_line=1,
        end_line=1,
        content="prior implementation",
        relevance="contract",
        source="batch_diff_support",
        derived_from_evidence_id=original["id"],
    )
    chunks, refs = build_context_evidence(
        [context.model_dump(mode="json")], "head", "base"
    )
    record_input = build_record_input(files, chunks, None, "head", "base")
    record_input["prior_evidence_bindings"] = [
        {key: value for key, value in original.items() if key != "content"}
    ]
    evidence_id = refs[0]["id"]
    summary = validate_record(
        UnitReviewRecord(
            change_summary="核验跨批次契约",
            target_checks=[],
            contract_dependencies=[
                UnitContractDependency(
                    file_path="a.py",
                    symbol="weighted_posterior",
                    assumption="实现与测试使用同一边界契约",
                    status="verified",
                    evidence_ids=[evidence_id],
                )
            ],
        ),
        record_input,
        {"a.py"},
    )

    assert summary.status == "reported", summary.reason
    assert summary.record.contract_dependencies[0].status == "verified"


def test_contract_dependency_scope_only_relaxes_same_primary_file() -> None:
    unit = ReviewUnit(
        id="u",
        primary_files=["a.py"],
        related_files=["b.py"],
        estimated_tokens=1,
        complexity=ReviewUnitComplexity.small,
        fingerprint="fp",
        grouping_reason="fixture",
    )
    assert contract_dependency_scope(
        UnitContractDependency(file_path="a.py", assumption="same", status="unresolved"),
        unit,
    ) == "intra_file"
    assert contract_dependency_scope(
        UnitContractDependency(file_path="b.py", assumption="related", status="unresolved"),
        unit,
    ) == "cross_file"
    assert contract_dependency_scope(
        UnitContractDependency(file_path="c.py", assumption="external", status="unresolved"),
        unit,
    ) == "cross_boundary"


def test_prompt_explicitly_carries_prior_contract_dependencies() -> None:
    record_input = build_record_input([_file()], [], None, "head", "base")
    record_input["prior_contract_dependencies"] = [{
        "file_path": "a.py",
        "symbol": "weighted_point",
        "assumption": "point must remain inside the interval",
        "status": "unresolved",
        "evidence_ids": [],
    }]
    prompt = OpenAICompatibleProvider._build_unit_review_prompt(
        PullRequestInfo.model_validate(_state()["pr_info"]),
        [_file()],
        "",
        record_input,
    )

    assert "prior_contract_dependencies" in prompt
    assert "Carry each obligation forward" in prompt


def _dependency() -> UnitContractDependency:
    return UnitContractDependency(file_path="a.py", symbol="weighted_point",
        assumption="边界契约在调用方成立", status="unresolved")


def _reported_summary(files, manifest, active, dependency) -> UnitReviewSummary:
    request = build_record_input(files, [], None, manifest.snapshot["head_sha"], manifest.snapshot["base_sha"])
    request["diff_manifest"] = manifest.model_dump(mode="json")
    request["working_memory"] = {}
    summary = validate_record(UnitReviewRecord(
        change_summary="检查当前活动范围",
        target_checks=[{"target": target, "status": "checked",
            "evidence_ids": [item.evidence_id for item in active.hunks if item.evidence_id],
            "reason": "逐个核对当前活动 Hunk"} for target in request["targets"]],
        contract_dependencies=[dependency]), request, {file.file_path for file in files})
    assert summary.status == "reported", summary.reason
    return summary


def _batch(unit, batch_id, files, manifest, active, dependency, *, status="completed", handoff=None):
    return UnitDiffBatch(
        id=batch_id,
        parent_unit_id=unit.id,
        input_hash=batch_id,
        version="unit-diff-worksets-v1",
        ranges=[],
        admission={},
        status=status,
        contract_handoff=handoff,
        result=ReviewUnitResult(review_unit_id=batch_id, status="completed",
            review_summary=_reported_summary(files, manifest, active, dependency),
            diff_manifest=manifest, active_evidence_set=active),
    )


def test_last_valid_record_obligation_reaches_next_batch_memory_and_handoff() -> None:
    dependency, files = _dependency(), [large_file(count=40, width=800)]
    unit = review_unit().model_copy(update={"primary_files": ["a.py"]})
    manifest = build_diff_manifest(unit, files, "head-a", "base-a")
    active = active_evidence_set(manifest, files, "batch-1")
    prior = _batch(unit, "batch-1", files, manifest, active, dependency, status="partial")
    # 本批次最近一次尝试没有有效记录，义务只保留在 last_valid_record。
    prior.result.review_summary = prior.result.review_summary.model_copy(
        update={"record": None, "status": "unknown", "latest_attempt_status": "failed"})
    entries = [prior, UnitDiffBatch(id="batch-2", parent_unit_id=unit.id, input_hash="two",
        version="unit-diff-worksets-v1", ranges=[], admission={})]

    memory = ReviewUnitExecutor._batch_memory(entries[:1], rebuild_coverage(manifest, entries[:1]))
    assert [item.model_dump(mode="json") for item in memory.last_valid_record.contract_dependencies] == [
        dependency.model_dump(mode="json")]

    support, handoff = ReviewUnitExecutor._batch_supporting_context(
        [(unit, files, {}), (unit, files, {})], entries, 1, manifest, files, "head-a", "base-a")

    # 两批覆盖同一范围，没有可携带正文，但义务仍必须被记录。
    assert support == []
    assert handoff["obligations"] == [contract_dependency_key(dependency)]
    assert handoff["carryable_paths"] == [] and handoff["dropped_obligations"] == []


def test_handoff_marks_dropped_obligation_only_when_carryable_body_was_left_out() -> None:
    dependency = _dependency()
    unit = review_unit().model_copy(update={"primary_files": ["a.py"]})
    small = [_file()]
    manifest = build_diff_manifest(unit, small, "head-a", "base-a")
    active = active_evidence_set(manifest, small, "batch-1")
    current = [small[0].model_copy(update={
        "hunks": [small[0].hunks[0].model_copy(update={"new_start": 20})]})]
    entries = [_batch(unit, "batch-1", small, manifest, active, dependency),
        UnitDiffBatch(id="batch-2", parent_unit_id=unit.id, input_hash="two",
            version="unit-diff-worksets-v1", ranges=[], admission={})]

    support, handoff = ReviewUnitExecutor._batch_supporting_context(
        [(unit, small, {}), (unit, current, {})], entries, 1, manifest, current, "head-a", "base-a")

    assert support and handoff["carryable_paths"] == ["a.py"]
    assert handoff["dropped_obligations"] == []

    # 同一义务下的前序正文超过单条整段上限：按行分块携带，而不是整段丢弃。
    huge = [large_file(count=40, width=800)]
    huge_manifest = build_diff_manifest(unit, huge, "head-a", "base-a")
    huge_active = active_evidence_set(huge_manifest, huge, "batch-1")
    huge_current = [huge[0].model_copy(update={
        "hunks": [huge[0].hunks[0].model_copy(update={"new_start": 900})]})]
    huge_entries = [_batch(unit, "batch-1", huge, huge_manifest, huge_active, dependency),
        UnitDiffBatch(id="batch-2", parent_unit_id=unit.id, input_hash="two",
            version="unit-diff-worksets-v1", ranges=[], admission={})]

    huge_support, huge_handoff = ReviewUnitExecutor._batch_supporting_context(
        [(unit, huge, {}), (unit, huge_current, {})], huge_entries, 1, huge_manifest,
        huge_current, "head-a", "base-a")

    assert huge_support
    assert huge_handoff["carryable_paths"] == ["a.py"]
    assert huge_handoff["dropped_obligations"] == []
    assert huge_support[0]["derived_from_evidence_id"] == huge_active.hunks[0].evidence_id
    assert "\n" in huge_support[0]["content"]  # 单条超限正文按行分块，而非整段丢弃
    assert huge_support[0]["end_line"] >= huge_support[0]["start_line"]


def test_chunked_support_slices_remain_citable_contract_evidence() -> None:
    dependency = _dependency()
    unit = review_unit().model_copy(update={"primary_files": ["a.py"]})
    huge = [large_file(count=40, width=800)]
    manifest = build_diff_manifest(unit, huge, "head-a", "base-a")
    active = active_evidence_set(manifest, huge, "batch-1")
    current = [huge[0].model_copy(update={
        "hunks": [huge[0].hunks[0].model_copy(update={"new_start": 900})]})]
    entries = [_batch(unit, "batch-1", huge, manifest, active, dependency),
        UnitDiffBatch(id="batch-2", parent_unit_id=unit.id, input_hash="two",
            version="unit-diff-worksets-v1", ranges=[], admission={})]

    support, _ = ReviewUnitExecutor._batch_supporting_context(
        [(unit, huge, {}), (unit, current, {})], entries, 1, manifest, current, "head-a", "base-a")

    chunks, _ = build_context_evidence(support, "head-a", "base-a")
    assert len(chunks) > 1
    original_id = support[0]["derived_from_evidence_id"]
    reference = next(item for item in entries[0].result.review_summary.evidence
                     if item.id == original_id)
    record_input = build_record_input(current, chunks, None, "head-a", "base-a")
    record_input["prior_evidence_bindings"] = [reference.model_dump(mode="json")]
    summary = validate_record(
        UnitReviewRecord(
            change_summary="分块携带的契约证据",
            contract_dependencies=[dependency.model_copy(update={
                "status": "checked", "evidence_ids": [chunks[-1]["evidence_id"]]})],
        ),
        record_input,
        {"a.py"},
    )

    assert summary.status == "reported", summary.reason
    assert summary.record.contract_dependencies[0].status == "checked"


def _empty_batch(batch_id, dependency=None, *, handoff=None):
    record = UnitReviewRecord(change_summary="检查本批范围",
        contract_dependencies=[dependency] if dependency else [])
    summary = UnitReviewSummary(status="reported", record=record, last_valid_record=record,
        latest_attempt_status="reported", reason="unit_diff_batches_complete")
    return UnitDiffBatch(id=batch_id, parent_unit_id="large", input_hash=batch_id,
        version="unit-diff-worksets-v1", ranges=[], admission={}, status="completed",
        target_coverage="partial", contract_handoff=handoff,
        result=ReviewUnitResult(review_unit_id=batch_id, status="completed", review_summary=summary))


def test_evidence_not_carried_requires_a_confirmed_dropped_handoff() -> None:
    dependency = _dependency()
    key = contract_dependency_key(dependency)
    unit = review_unit().model_copy(update={"primary_files": ["a.py"], "related_files": ["b.py"]})
    assert key == "a.py:weighted_point:边界契约在调用方成立"

    carried = ReviewUnitExecutor._aggregate_diff_batches(unit, [
        _empty_batch("batch-1", dependency, handoff={"obligations": [key],
            "carryable_paths": ["a.py"], "dropped_obligations": [key]}),
        _empty_batch("batch-2", dependency),
    ], ExecutionBudget(), "binding")
    assert carried.input_coverage.blocking_dependencies == [key]
    assert carried.input_coverage.reason == "evidence_not_carried_to_next_batch"

    late = ReviewUnitExecutor._aggregate_diff_batches(unit, [
        _empty_batch("batch-1"),
        _empty_batch("batch-2", dependency),
    ], ExecutionBudget(), "binding")
    assert late.input_coverage.blocking_dependencies == [key]
    assert late.input_coverage.reason == "contract_dependency_unresolved"


def test_diagnosis_input_carries_obligation_without_bound_support() -> None:
    dependency = _dependency()
    record = UnitReviewRecord(change_summary="前序检查", contract_dependencies=[dependency])
    memory = UnitReviewSummary(status="reported", record=record, last_valid_record=record,
        latest_attempt_status="reported")
    unit = review_unit().model_copy(update={"primary_files": ["a.py"]})
    state = {"unit": unit, "unit_files": [_file()], "context": [], "unit_plan": None, "unit_diff": "",
        "parent_state": {"pr_info": _state()["pr_info"], "head_sha": "head-a",
                         "batch_memory_summary": memory}}
    runner = ReviewUnitExecutor(WorksetProvider(), concurrency=1, timeout_seconds=10)

    record_input = runner._diagnosis_args(state)[-1]

    assert record_input["prior_contract_dependencies"] == [dependency.model_dump(mode="json")]
    assert "contract_dependencies" not in record_input["working_memory"]
    prompt = OpenAICompatibleProvider._build_unit_review_prompt(
        PullRequestInfo.model_validate(state["parent_state"]["pr_info"]),
        state["unit_files"],
        "",
        record_input,
    )
    assert "prior_contract_dependencies" in prompt
