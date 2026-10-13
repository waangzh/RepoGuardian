import asyncio

import pytest

from app.agents.providers import LLMProviderError, OpenAICompatibleProvider
from app.models.review import (AgentAction, ChangedFile, ChangedLine, DiffHunk, DiffLine,
    ExecutionBudget, ReviewIssue, ReviewUnit, ReviewUnitResult, UnitDiffBatch,
    UnitReviewRecord, UnitReviewResponse)
from app.review.unit_completion import is_review_unit_complete, is_reusable_review_unit_result
from app.services.model_request_budgeter import ModelRequestProfile, request_budget_reserver
from app.services.review_unit_executor import ReviewUnitExecutor
from app.services.unit_worksets import MAX_UNIT_WORKSETS, build_unit_worksets
from test_unit_review_summary import _state


def large_file(kind="added", count=100, width=800):
    lines = [DiffLine(kind=kind, content=f"{i}:" + "x" * width,
        old_line_no=i + 1 if kind != "added" else None,
        new_line_no=i + 1 if kind != "deleted" else None) for i in range(count)]
    hunk = DiffHunk(old_start=1, old_length=count if kind != "added" else 0,
        new_start=1, new_length=count if kind != "deleted" else 0, lines=lines,
        added_lines=[ChangedLine(line_no=line.new_line_no, content=line.content) for line in lines if kind == "added"],
        removed_lines=[ChangedLine(line_no=line.old_line_no, content=line.content) for line in lines if kind == "deleted"])
    return ChangedFile(file_path="a.py", change_type="modified", additions=count if kind == "added" else 0,
        deletions=count if kind == "deleted" else 0, hunks=[hunk])


def review_unit():
    return ReviewUnit(id="large", primary_files=["a.py"], estimated_tokens=300000,
        complexity="large", fingerprint="large-fp", grouping_reason="synthetic-large")


def test_batch_execution_status_is_separate_from_target_coverage():
    batch = UnitDiffBatch(
        id="batch-1",
        parent_unit_id="large",
        input_hash="hash",
        version="unit-diff-worksets-v1",
        ranges=[],
        admission={},
        status="partial",
        reason="review_step_no_evidence_progress",
        target_coverage="complete",
        contract_coverage="pending",
    )

    assert batch.batch_execution_status == "no_new_context"
    assert batch.target_coverage == "complete"
    assert batch.contract_coverage == "pending"


class WorksetProvider(OpenAICompatibleProvider):
    def __init__(self, failure=None):
        super().__init__("key", "https://example.test/v1", "unknown-model",
            request_profile=ModelRequestProfile(context_window=50000))
        self.requests = []
        self.failure = failure
        self.decisions = 0

    async def review_unit(self, pr, files, diff, model, record_input):
        metadata = self.unit_diagnosis_admission(pr, files, diff, model, record_input)
        await request_budget_reserver.get()(metadata)
        self.requests.append((files, record_input, metadata))
        if len(self.requests) == 2 and self.failure == "error":
            raise LLMProviderError("fixture_transport_failure")
        if len(self.requests) == 2 and self.failure == "timeout":
            await asyncio.sleep(1)
        self._unit_evidence_payload(files, record_input)
        record = UnitReviewRecord(change_summary="检查当前批次", target_checks=[{
            "target": target, "status": "checked", "reason": "核对当前完整证据",
            "evidence_ids": [item["id"] for item in record_input["evidence"]],
        } for target in record_input["targets"]])
        if len(self.requests) == 2 and self.failure == "missing":
            record = None
        if len(self.requests) == 2 and self.failure == "unchecked":
            record = UnitReviewRecord(change_summary="未检查本批目标")
        if self.failure == "unresolved":
            record = UnitReviewRecord.model_validate({**record.model_dump(), "unresolved_questions": [{
                "question": "其他批次的约束仍待核验", "affected_files": ["a.py"],
                "evidence_ids": [record_input["evidence"][0]["id"]]}]})
        issues = []
        if len(self.requests) == 1 and self.failure in {"error", "missing", "timeout"}:
            issues = [ReviewIssue(review_unit_id="large", title="候选缺陷", category="correctness", severity="high",
                confidence=0.8, affected_behavior="可能出现空值错误", failure_scenario="输入为空时发生失败",
                recommendation="检查空值", primary_evidence={"file_path": "a.py",
                    "existing_code": files[0].hunks[0].lines[0].content})]
        return UnitReviewResponse(review_record=record, issues=issues)

    async def decide(self, state, model):
        # 不调用远端服务；依然通过真实 Unit ledger 计费。
        await request_budget_reserver.get()({"reserved_tokens": 1000})
        self.decisions += 1
        if self.failure == "human":
            return AgentAction(action="request_human", reason="缺少业务约束", human_request={
                "missing_information": ["接口契约"], "known_evidence": ["当前批次 Diff"],
                "questions": ["接口是否允许空值？"], "prohibited_operations": ["运行目标仓库代码"]})
        return AgentAction(action="task_done" if state["issue_round_completed"] else "report_issue",
                           reason="当前工作集检查")

    async def plan_review_unit(self, state, model):
        raise LLMProviderError("fixture_optional_plan_unavailable")


def runner_and_state(provider=None, *, calls=64, attempts=32, tokens=1000000, timeout=10):
    provider = provider or WorksetProvider()
    runner = ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=timeout)
    runner._budget_for = lambda unit, **_kwargs: ExecutionBudget(max_model_calls=calls,
        max_diagnosis_attempts=attempts, max_token_usage=tokens, max_context_retrievals=12)
    state = _state()
    state["changed_files"] = [large_file().model_dump(mode="json")]
    return runner, state


@pytest.mark.asyncio
async def test_oversized_single_hunk_reviews_every_line_with_real_request_admission():
    runner, state = runner_and_state()
    result = await runner.execute_unit(review_unit(), state)
    assert len(result.diff_batches) > 1
    assert all(entry.status == "completed" for entry in result.diff_batches), result.model_dump()
    assert is_review_unit_complete(result) and is_reusable_review_unit_result(result)
    assert result.review_unit_id == "large"
    seen = {line.new_line_no for files, _, _ in runner.provider.requests
            for file in files for hunk in file.hunks for line in hunk.lines}
    assert seen == set(range(1, 101))
    assert all(item[2]["reserved_tokens"] + 512 <= 50000 for item in runner.provider.requests)
    assert result.execution_budget.diagnosis_attempts == len(runner.provider.requests)
    assert result.execution_budget.token_usage <= result.execution_budget.max_token_usage
    assert ReviewUnitResult.model_validate_json(result.model_dump_json()).diff_batches == result.diff_batches
    catalog = {item.id for item in result.review_summary.evidence}
    assert all(set(check.evidence_ids) <= catalog for check in result.review_summary.record.target_checks)
    from app.services.cross_unit_coordination import CrossUnitCoordinationService
    payload = CrossUnitCoordinationService.catalog({**state, "review_units": [review_unit().model_dump(mode="json")],
        "review_unit_results": [result.model_dump(mode="json")], "cross_unit_risk": {}})
    assert catalog == {item["id"] for item in payload["evidence"]}
    assert catalog == set(payload["summaries"][0]["summary"]["evidence_ids"])


@pytest.mark.parametrize("kind", ["added", "deleted", "context"])
def test_fragment_coordinates_and_coverage_preserve_both_sides(kind):
    file = large_file(kind=kind, count=30, width=8)
    batches = build_unit_worksets(review_unit(), [file], lambda unit, files, metadata: {
        "admitted": sum(len(hunk.lines) for file in files for hunk in file.hunks) <= 12,
        "reason": "model_context_window_exceeded"})
    offsets = set()
    for _, files, metadata in batches:
        interval = metadata["ranges"][0]
        offsets.update(range(interval["start_offset"], interval["end_offset"]))
        hunk = files[0].hunks[0]
        assert hunk.lines == file.hunks[0].lines[interval["start_offset"]:interval["end_offset"]]
        if kind != "added":
            assert hunk.old_start == hunk.lines[0].old_line_no
        if kind != "deleted":
            assert hunk.new_start == hunk.lines[0].new_line_no
    assert offsets == set(range(30))
    assert [item[2]["input_hash"] for item in batches] == [item[2]["input_hash"] for item in
        build_unit_worksets(review_unit(), [file], lambda unit, files, metadata: {
            "admitted": len(files[0].hunks[0].lines) <= 12, "reason": "model_context_window_exceeded"})]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "missing", "timeout", "unchecked"])
async def test_batch_failure_does_not_erase_prior_progress_or_claim_complete(failure):
    runner, state = runner_and_state(WorksetProvider(failure), timeout=1)
    result = await runner.execute_unit(review_unit(), state)
    assert result.diff_batches[0].status == "completed"
    assert any(item.status != "completed" for item in result.diff_batches)
    assert not is_review_unit_complete(result) and not is_reusable_review_unit_result(result)
    assert result.input_coverage.target_coverage == "partial"
    assert result.review_summary.status == "unknown"
    assert result.execution_budget.model_calls >= 2
    if failure != "unchecked":
        assert result.issues and result.issues[0].review_unit_id == "large"
    assert result.execution_budget.token_usage == (sum(request[2]["reserved_tokens"]
        for request in runner.provider.requests) + 1000 * runner.provider.decisions)


@pytest.mark.asyncio
async def test_complete_batches_restore_without_reissuing_or_rebilling():
    runner, state = runner_and_state()
    first = await runner.execute_unit(review_unit(), state)
    assert is_reusable_review_unit_result(first)
    calls = len(runner.provider.requests)
    state["review_unit_results"] = [first.model_dump(mode="json")]
    second = await runner.execute_unit(review_unit(), state)
    assert is_reusable_review_unit_result(second)
    assert len(runner.provider.requests) == calls
    assert second.execution_budget == first.execution_budget
    assert all(batch.reason == "restored_valid_batch" for batch in second.diff_batches)


@pytest.mark.asyncio
async def test_failed_batch_retries_only_missing_coverage_and_keeps_spent_budget():
    runner, state = runner_and_state(WorksetProvider("error"))
    first = await runner.execute_unit(review_unit(), state)
    assert not is_reusable_review_unit_result(first)
    calls = len(runner.provider.requests)
    runner.provider.failure = None
    state["review_unit_results"] = [first.model_dump(mode="json")]
    second = await runner.execute_unit(review_unit(), state)
    assert is_reusable_review_unit_result(second)
    assert len(runner.provider.requests) == calls + 1
    assert second.execution_budget.model_calls == first.execution_budget.model_calls + 2


@pytest.mark.asyncio
async def test_legacy_human_request_does_not_pause_parent_or_remaining_batches():
    runner, state = runner_and_state(WorksetProvider("human"))
    result = await runner.execute_unit(review_unit(), state)
    assert result.status == "completed" and result.human_request is None
    assert len(runner.provider.requests) == len(result.diff_batches)
    assert all(batch.status == "completed" for batch in result.diff_batches)
    assert is_review_unit_complete(result)


@pytest.mark.asyncio
async def test_unresolved_cross_batch_contracts_do_not_claim_complete_review():
    runner, state = runner_and_state(WorksetProvider("unresolved"))
    result = await runner.execute_unit(review_unit(), state)
    assert all(batch.status == "completed" for batch in result.diff_batches)
    assert result.input_coverage.evidence_coverage == "complete"
    assert result.input_coverage.target_coverage == "complete"
    assert result.input_coverage.contract_coverage == "partial"
    assert all(batch.target_coverage == "complete" for batch in result.diff_batches)
    assert all(batch.contract_coverage == "pending" for batch in result.diff_batches)
    assert result.unit_completion_status == "incomplete"
    assert result.input_coverage.unresolved_questions or result.input_coverage.unresolved_dependencies
    assert result.review_summary.status == "reported"
    assert result.review_summary.last_valid_record.unresolved_questions
    assert not is_review_unit_complete(result)


@pytest.mark.asyncio
async def test_batch_calls_share_budget_and_preserve_unexecuted_manifest():
    runner, state = runner_and_state(calls=2, attempts=2)
    result = await runner.execute_unit(review_unit(), state)
    assert result.diff_batches[0].status == "completed"
    assert result.execution_budget.max_model_calls == 2
    assert result.execution_budget.max_diagnosis_attempts == 2
    assert not is_review_unit_complete(result)
    assert result.execution_budget.model_calls <= 2
    assert result.execution_budget.diagnosis_attempts <= 2
    assert any(item.status == "skipped" for item in result.diff_batches)
    assert result.input_coverage.omitted_targets


@pytest.mark.asyncio
async def test_single_overlong_line_is_explicitly_unreviewed_not_sliced():
    runner, state = runner_and_state()
    state["changed_files"] = [large_file(count=1, width=100000).model_dump(mode="json")]
    result = await runner.execute_unit(review_unit(), state)
    assert not runner.provider.requests
    assert result.diff_batches[0].status == "skipped"
    assert result.diff_batches[0].reason == "unsplittable_diff_input"
    assert not is_review_unit_complete(result)


def test_workset_count_limit_keeps_every_range_including_skipped_ranges():
    file = large_file(count=1000, width=8)
    batches = build_unit_worksets(review_unit(), [file], lambda *args: {
        "admitted": False, "reason": "model_context_window_exceeded"})
    assert len(batches) <= MAX_UNIT_WORKSETS
    seen = {offset for _, _, entry in batches for interval in entry["ranges"]
            for offset in range(interval["start_offset"], interval["end_offset"])}
    assert seen == set(range(1000))


def test_restoration_preserves_original_hunk_id_and_rejects_tampered_ranges():
    from app.models.review import UnitDiffBatch
    from app.services.unit_worksets import restore_unit_workset_files
    file = large_file(count=30, width=8)
    file.hunks[0].hunk_id = "original-hunk"
    batches = build_unit_worksets(review_unit(), [file], lambda unit, files, metadata: {
        "admitted": len(files[0].hunks[0].lines) <= 12, "reason": "model_context_window_exceeded"})
    for _, selected, metadata in batches:
        batch = UnitDiffBatch(**metadata)
        assert selected[0].hunks[0].hunk_id == "original-hunk"
        assert restore_unit_workset_files(review_unit(), [file], batch) == selected
        batch.ranges[0]["end_offset"] += 1
        with pytest.raises(ValueError, match="invalid_unit_workset_hash"):
            restore_unit_workset_files(review_unit(), [file], batch)


@pytest.mark.asyncio
async def test_small_unit_keeps_single_workset_path():
    runner, state = runner_and_state()
    state["changed_files"] = [large_file(count=2, width=8).model_dump(mode="json")]
    result = await runner.execute_unit(review_unit(), state)
    assert result.diff_batches == []


def test_multifile_partition_keeps_complete_file_and_hunk_inventory():
    files = [large_file(count=2, width=8).model_copy(update={"file_path": f"path/{index}.py"}) for index in range(17)]
    unit = review_unit().model_copy(update={"primary_files": [file.file_path for file in files]})
    batches = build_unit_worksets(unit, files, lambda unit, files, metadata: {
        "admitted": len(files) <= 3, "reason": "required_input_too_large"})
    assert len(batches) > 1
    assert all(metadata["admission"]["admitted"] for _, _, metadata in batches)
    assert [file.file_path for _, batch_files, _ in batches for file in batch_files] == unit.primary_files
