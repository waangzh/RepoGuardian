import pytest

from app.evaluation.runner import final_audit
from app.models.review import ReviewIssue, ReviewUnitResult, UnitReviewSummary, UnitReviewRecord
from app.review.unit_completion import is_review_unit_complete, is_review_unit_execution_complete
from app.services.issue_deduplication import IssueDeduplicationService


def test_provider_issue_passes_filter_and_batch_aggregation_with_parent_source():
    from app.models.review import ReviewUnit, UnitDiffBatch, ExecutionBudget
    from app.services.review_planner import DeterministicReviewPlanner
    from app.services.review_unit_executor import ReviewUnitExecutor

    child = ReviewUnit(id="child", primary_files=["a.py"], fingerprint="child", grouping_reason="fixture",
                       estimated_tokens=10, complexity="small")
    parent = child.model_copy(update={"id": "parent", "fingerprint": "parent"})
    scope = DeterministicReviewPlanner().build_scope(child, None, repository_files={"a.py"})
    filtered = ReviewUnitExecutor._filter_issues([issue("i")], child, scope)
    assert filtered[0].source_review_unit_ids == ["child"]
    summary = UnitReviewSummary(status="reported", record=UnitReviewRecord(change_summary="检查完成"),
                               latest_attempt_status="reported")
    result = ReviewUnitResult(review_unit_id="child", status="completed", issues=filtered, review_summary=summary)
    batch = UnitDiffBatch(id="child", parent_unit_id="parent", input_hash="hash", ranges=[],
                         version="unit-diff-worksets-v1", admission={}, status="completed", result=result)
    aggregated = ReviewUnitExecutor._aggregate_diff_batches(parent, [batch], ExecutionBudget(), "binding")
    assert aggregated.issues[0].review_unit_id == "parent"
    assert aggregated.issues[0].source_review_unit_ids == ["parent"]


async def test_dedup_node_records_intermediate_canonical_unit_for_two_hops():
    from types import SimpleNamespace
    from app.graph.nodes.issue_validation import issue_deduplication_node
    from app.models.review import IssueDeduplicationDecision, IssueMetrics, IssueStatus
    from app.review.issue_audit import capture_issue_audit

    original = [issue("A", "u1"), issue("B", "u2"), issue("C", "u3")]
    canonical = original[2].model_copy(update={"status": IssueStatus.confirmed})
    decisions = [IssueDeduplicationDecision(canonical_issue_id="B", duplicate_issue_ids=["A"], merged_rationale="same"),
                 IssueDeduplicationDecision(canonical_issue_id="C", duplicate_issue_ids=["B"], merged_rationale="same")]

    class Dedup:
        async def aggregate(self, *args):
            return SimpleNamespace(issues=[canonical], decisions=decisions, metrics=IssueMetrics(), model_usages=[])

    events = [event("schema_validation", "u1", "schema", "A")]
    with capture_issue_audit(events.append):
        state = await issue_deduplication_node({"review_issues": [raw.model_dump(mode="json") for raw in original],
            "_provider": object(), "_issue_deduplication_service": Dedup()})
    final = next(row for row in final_audit(events, state, "completed")
                 if row["stage"] == "final" and row["issue_id"] == "schema")
    assert final["status"] == "confirmed" and final["canonical_id"] == "C"
    assert final["canonical_review_unit_id"] == "u3"


def issue(identity, unit="unassigned"):
    return ReviewIssue(id=identity, review_unit_id=unit, title="返回空值", category="correctness",
        severity="medium", confidence=.9, affected_behavior="调用方读取失败", failure_scenario="空值路径导致失败",
        recommendation="处理空值", primary_evidence={"file_path": "a.py", "existing_code": "return None"})


def test_issue_source_follows_child_parent_and_global_dedup():
    first = issue("i").for_review_unit("child").for_review_unit("parent")
    assert first.review_unit_id == "parent" and first.source_review_unit_ids == ["parent"]
    second = issue("j", "other")
    merged = IssueDeduplicationService._merge(first, [second])
    assert merged.source_review_unit_ids == ["parent", "other"]
    assert merged.for_review_unit("new_parent").source_review_unit_ids == ["new_parent", "other"]
    assert merged.source_issue_ids == ["i", "j"]


@pytest.mark.parametrize("summary", [UnitReviewSummary(), UnitReviewSummary(input_protocol="legacy"),
    UnitReviewSummary(reason="missing_review_record", latest_attempt_status="missing"),
    UnitReviewSummary(reason="invalid", latest_attempt_status="invalid")])
def test_missing_or_invalid_record_never_counts_as_complete(summary):
    result = ReviewUnitResult(review_unit_id="u", status="completed", terminal_reason="no_issue", review_summary=summary)
    assert is_review_unit_execution_complete(result)
    assert not is_review_unit_complete(result)


def test_reported_record_retains_completion():
    summary = UnitReviewSummary(status="reported", record=UnitReviewRecord(change_summary="已检查"),
                               latest_attempt_status="reported")
    assert is_review_unit_complete(ReviewUnitResult(review_unit_id="u", status="completed", review_summary=summary))


def event(stage, unit, identity, canonical=None, canonical_unit=None):
    return {"stage": stage, "review_unit_id": unit, "issue_id": identity, "canonical_id": canonical,
            "canonical_review_unit_id": canonical_unit, "status": "candidate", "reason": None}


@pytest.mark.parametrize("status", ["confirmed", "unresolved", "dismissed"])
def test_cross_unit_two_hop_schema_inherits_terminal_with_explicit_identity(status):
    events = [event("schema_validation", "u1", "A", "B"),
              event("deduplication", "u1", "B", "C", "u2"), event("generated", "u2", "C")]
    state = {"review_issues": [{"id": "C", "review_unit_id": "u2", "status": status}]}
    final = {row["issue_id"]: row for row in final_audit(events, state, "completed") if row["stage"] == "final"}
    assert final["A"]["status"] == final["A"]["canonical_status"] == status
    assert final["A"]["canonical_id"] == "C" and final["A"]["canonical_review_unit_id"] == "u2"
    assert final["B"]["status"] == "merged" and final["B"]["canonical_status"] == status


@pytest.mark.parametrize("edges,reason", [([("A", "B"), ("B", "A")], "alias_cycle"),
                                        ([("A", "B"), ("A", "C")], "alias_ambiguous")])
def test_invalid_alias_never_inherits_success(edges, reason):
    events = [event("identity", "u", source, target) for source, target in edges]
    state = {"review_issues": [{"id": "B", "review_unit_id": "u", "status": "confirmed"}]}
    final = next(row for row in final_audit(events, state, "completed")
                 if row["stage"] == "final" and row["issue_id"] == "A")
    assert final["status"] == "not_published" and final["reason"] == reason
    assert final["canonical_id"] is None and final["canonical_status"] is None


def test_unrelated_same_id_cannot_supply_cross_unit_terminal():
    events = [event("schema_validation", "u1", "A", "B")]
    state = {"review_issues": [{"id": "B", "review_unit_id": "u2", "status": "confirmed"}]}
    final = next(row for row in final_audit(events, state, "completed")
                 if row["stage"] == "final" and row["issue_id"] == "A")
    assert final["canonical_status"] is None
