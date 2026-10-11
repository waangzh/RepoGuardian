import json
from types import SimpleNamespace

import pytest

from app.models.review import (AgentAction, ExecutionBudget, IssueDeduplicationDecision, IssueMetrics,
    ReviewUnitResult, UnitCoverageCheck, UnitInputCoverage, UnitReviewRecord)
from app.review.unit_completion import is_review_unit_complete
from app.services.issue_deduplication import IssueDeduplicationService
from app.services.issue_verifier import IssueVerifierService
from app.services.review_unit_executor import ReviewUnitExecutor
from app.services.unit_coverage import (active_evidence_set, build_diff_manifest, new_coverage_ledger, update_coverage)
from app.services.unit_review_summary import build_record_input, validate_record
from app.tools.diff_parser import DiffParser
from test_issue_validation import _issue, _unit
from test_unit_review_summary import _file


@pytest.mark.parametrize("hunk_incomplete", [False, True])
async def test_target_checks_and_contract_obligations_have_separate_coverage(hunk_incomplete):
    unit, files = _unit(path="a.py"), [_file()]
    manifest = build_diff_manifest(unit, files, "head", "base")
    active = active_evidence_set(manifest, files, unit.id)
    record_input = build_record_input(files, [], None, "head", "base")
    identity = record_input["evidence"][0]["id"]
    summary = validate_record(UnitReviewRecord(change_summary="检查返回值", target_checks=[{
        "target": record_input["targets"][0], "status": "checked", "reason": "核对代码", "evidence_ids": [identity]}],
        unresolved_questions=[{"question": "调用方契约未知", "affected_files": ["a.py"], "evidence_ids": [identity]}]),
        record_input, {"a.py"})
    ledger = update_coverage(new_coverage_ledger(manifest), manifest, active, summary)
    ledger.dependencies["dep"] = UnitCoverageCheck(status="unresolved")
    if hunk_incomplete:
        ledger.diff_hunks[manifest.hunks[0].id].status = "needs_followup"
    executor = ReviewUnitExecutor(object(), concurrency=1, timeout_seconds=5)
    update = await executor._finish_unit_node({"next_action": AgentAction(action="task_done", reason="检查结束"),
        "unit": unit, "unit_files": files, "context": [], "parent_state": {}, "diff_manifest": manifest,
        "coverage_ledger": ledger, "issues": [], "messages": [], "issue_round_completed": True,
        "budget": ExecutionBudget(), "input_coverage": UnitInputCoverage(target_coverage="complete")})
    coverage = update["input_coverage"]
    assert coverage.target_coverage == ("partial" if hunk_incomplete else "complete")
    assert coverage.contract_coverage == "partial"
    assert coverage.unresolved_dependencies == ["dep"] and coverage.unresolved_questions
    assert "dep" not in coverage.omitted_targets
    assert not set(coverage.unresolved_questions) & set(coverage.omitted_targets)
    result = ReviewUnitResult(review_unit_id=unit.id, status="completed", input_coverage=coverage,
        review_summary=summary, diff_manifest=manifest, coverage_ledger=ledger)
    assert not is_review_unit_complete(result)


def test_verifier_diff_has_single_body_with_both_sides():
    diff = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def value():\n-    return old\n+    return new\n"
    files = DiffParser().parse(diff)
    state = {"changed_files": [file.model_dump(mode="json") for file in files]}
    request = IssueVerifierService(object(), enabled=True, fail_mode="candidate", max_calls_per_unit=4)._build_request(_issue("i"), _unit(), state, 0)
    hunk = json.loads(request.unit_diff)[0]["hunks"][0]
    assert hunk["lines"] and "added_lines" not in hunk and "removed_lines" not in hunk
    sources = IssueVerifierService._code_sources(request)
    assert any("return new" in source for source in sources[("app.py", "head")])
    assert any("return old" in source for source in sources[("app.py", "base")])


@pytest.mark.parametrize("contract", ["partial", "unknown"])
def test_contract_gap_without_ledger_still_blocks_overall_completion(contract):
    from app.models.review import UnitReviewSummary
    result = ReviewUnitResult(review_unit_id="u", status="completed",
        review_summary=UnitReviewSummary(status="reported", latest_attempt_status="reported",
                                        record=UnitReviewRecord(change_summary="检查完成")),
        input_coverage=UnitInputCoverage(target_coverage="complete", contract_coverage=contract))
    assert not is_review_unit_complete(result)


def test_aggregate_failure_preserves_candidates_but_cannot_be_complete():
    from app.models.review import UnitDiffBatch, UnitReviewSummary
    child = ReviewUnitResult(review_unit_id="child", status="completed", issues=[_issue("i", unit_id="child")],
        review_summary=UnitReviewSummary(status="reported", latest_attempt_status="reported",
                                        record=UnitReviewRecord(change_summary="检查完成")))
    batch = UnitDiffBatch(id="child", parent_unit_id="u", input_hash="fixture", ranges=[],
        version="unit-diff-worksets-v1", admission={}, status="completed", result=child)
    result = ReviewUnitExecutor._aggregate_diff_batches(_unit("u"), [batch], ExecutionBudget(), "fixture",
                                                       reason="review_unit_timed_out")
    assert result.issues
    assert result.review_summary.latest_attempt_status == "failed"
    assert not is_review_unit_complete(result)


def test_oversized_verifier_diff_retains_valid_json_and_no_partial_hunk():
    file = _file().model_copy(update={"file_path": "app.py"}, deep=True)
    file.hunks[0].added_lines[0].content = "x" * 70_000
    request = IssueVerifierService(object(), enabled=True, fail_mode="candidate", max_calls_per_unit=4)._build_request(_issue("i"), _unit(), {
        "changed_files": [file.model_dump(mode="json")]}, 0)
    assert not request.evidence_complete
    assert len(request.unit_diff) <= 60_000
    assert json.loads(request.unit_diff)[0]["hunks"] == []


@pytest.mark.parametrize("merge", [False, True])
async def test_nearby_same_symbol_different_wording_requires_semantic_decision(merge):
    candidates = [_issue("first", status="confirmed", anchor_hash="a"),
                  _issue("second", status="confirmed", anchor_hash="b", unit_id="followup")]
    for index, issue in enumerate(candidates):
        issue.primary_evidence = issue.primary_evidence.model_copy(update={
            "expected_hunk_id": "h", "resolved_start_line": 107 + index, "resolved_side": "head"})
        issue.affected_behavior = "未初始化掩码" if index else "未来 token 未屏蔽"
    calls = []
    async def decide(issues, model):
        calls.append([issue.id for issue in issues])
        return IssueDeduplicationDecision(canonical_issue_id="first", duplicate_issue_ids=["second"] if merge else [],
                                         merged_rationale="相同根因" if merge else "不同根因")
    result = await IssueDeduplicationService().aggregate(candidates, SimpleNamespace(deduplicate_issues=decide), None, IssueMetrics())
    assert calls == [["first", "second"]]
    assert len(result.issues) == (1 if merge else 2)
    if merge:
        assert result.issues[0].source_review_unit_ids == ["unit-1", "followup"]
