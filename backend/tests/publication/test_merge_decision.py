import pytest

from app.models.publication import (
    PublicationPlan, PublicationSnapshot, ReviewDigest, ReviewOutcome,
)
from app.review.models import CandidateFinding, VerificationResult, VerifiedFinding
from app.services.publication.publication_renderer import (
    discussion_report_lines, render_review_comment, render_summary,
)
from app.services.publication.review_merge_decision import merge_decision
from app.services.publication.review_presentation import build_presentation


def outcome(**updates):
    return ReviewOutcome(**({
        "kind": "clean", "coverage": "complete", "review_completeness": "complete",
        "runtime_reliability": "reliable", "examined_goals": 2, "total_goals": 2,
        "finding_conclusion": "no_confirmed_findings",
    } | updates))


def finding(severity, identity="finding", attribution="introduced"):
    candidate = CandidateFinding(
        id=identity, assignment_ids=["behavior"], title="Regression", trigger="Input",
        expected_behavior="Value 1", actual_behavior="Value 2", impact="Incorrect value",
        recommendation="Restore contract", severity=severity, affected_path="safe.py",
        line=1, evidence_ids=["source"], attribution=attribution,
    )
    return VerifiedFinding(
        id=identity, candidate=candidate, evidence_ids=["source"], inline_line=1,
        verification=VerificationResult(
            candidate_id=identity, decision="confirmed", explanation="Source confirmed",
            evidence_ids=["source"], attribution=attribution,
        ),
    )


@pytest.mark.parametrize("severity,status", [
    ("critical", "fix_blockers"), ("high", "fix_blockers"),
    ("medium", "conditional_merge"), ("low", "conditional_merge"),
])
def test_current_confirmed_findings_determine_priority(severity, status):
    result = merge_decision(
        outcome(kind="findings", confirmed_count=1, publishable_count=1), [finding(severity)],
        content_eligible=True, execution_complete=True,
    )
    assert result.status == status
    assert result.blocking_finding_keys == (["finding"] if status == "fix_blockers" else [])
    assert result.nonblocking_finding_keys == ([] if status == "fix_blockers" else ["finding"])
    assert "1" in result.label
    assert result.unresolved_reasons == []


@pytest.mark.parametrize("facts,execution_complete,reason", [
    ({"unresolved_count": 1}, True, "verification_unresolved"),
    ({"coverage": "partial"}, True, "scope_incomplete"),
    ({"review_completeness": "unknown"}, True, "scope_incomplete"),
    ({"examined_goals": 1}, True, "scope_incomplete"),
    ({"total_goals": 0, "examined_goals": 0}, True, "scope_empty"),
    ({}, False, "termination_incomplete"),
    ({"runtime_reliability": "unreliable"}, True, "termination_incomplete"),
    ({"confirmed_count": 1, "publishable_count": 0}, True, "confirmed_findings_withheld"),
])
def test_gaps_prevent_clean_advice_even_without_precomputed_reasons(facts, execution_complete, reason):
    result = merge_decision(
        outcome(**facts), [], content_eligible=True, execution_complete=execution_complete,
    )
    assert result.status == "resolve_unknowns"
    assert reason in result.unresolved_reasons
    assert result.limits


def test_blockers_and_ordinary_findings_do_not_hide_unresolved_or_incomplete_work():
    result = merge_decision(
        outcome(kind="findings", confirmed_count=2, publishable_count=2, unresolved_count=1,
                examined_goals=1, coverage="partial", review_completeness="partial"),
        [finding("high"), finding("medium", "ordinary")],
        content_eligible=True, execution_complete=False,
    )
    assert result.status == "fix_blockers"
    assert result.nonblocking_finding_keys == ["ordinary"]
    assert set(result.unresolved_reasons) == {
        "scope_incomplete", "verification_unresolved", "termination_incomplete",
    }
    ordinary = merge_decision(
        outcome(kind="findings", confirmed_count=1, publishable_count=1, unresolved_count=1),
        [finding("medium")], content_eligible=True, execution_complete=True,
    )
    assert ordinary.status == "resolve_unknowns"
    assert ordinary.nonblocking_finding_keys == ["finding"]


@pytest.mark.parametrize("kind,eligible", [
    ("failed", True), ("cancelled", True), ("findings", False),
])
def test_failure_cancellation_or_content_rejection_overrides_confirmed_blockers(kind, eligible):
    result = merge_decision(
        outcome(kind=kind, confirmed_count=1, publishable_count=1,
                reason_codes=[] if eligible else ["snapshot_missing"]),
        [finding("high")], content_eligible=eligible, execution_complete=True,
    )
    assert result.status == "unavailable"
    assert result.blocking_finding_keys == ["finding"]
    if not eligible:
        assert "snapshot_missing" in result.unresolved_reasons


def test_usage_unknown_is_preserved_without_blocking_complete_code_review():
    facts = outcome(kind="inconclusive", reason_codes=["usage_integrity_unknown"],
                    runtime_reliability="unknown")
    plan = PublicationPlan(
        review_id="review", snapshot=PublicationSnapshot(), outcome=facts, content_eligible=True,
    )
    plan.digest.merge_decision = merge_decision(
        facts, [], content_eligible=True, execution_complete=True,
    )
    assert plan.digest.merge_decision.status == "conditional_merge"
    assert plan.digest.merge_decision.unresolved_reasons == []
    assert facts.reason_codes == ["usage_integrity_unknown"]
    for text in (render_summary(plan), render_review_comment(plan)):
        assert "计量" in text
        assert "未知用量不能当作完整统计" in text


def test_preexisting_findings_use_current_identity_without_separate_history_counts():
    current = finding("high", attribution="pre_existing")
    result = merge_decision(
        outcome(kind="findings", confirmed_count=1, publishable_count=1), [current, current],
        content_eligible=True, execution_complete=True,
    )
    assert result.status == "fix_blockers"
    assert result.blocking_finding_keys == ["finding"]
    assert "1 项" in result.label


def test_discussion_context_renders_current_collection_stats_without_claiming_rechecks():
    plan = PublicationPlan(
        review_id="review", snapshot=PublicationSnapshot(), outcome=outcome(),
        content_eligible=True, digest=ReviewDigest(
            discussion_review_enabled=True,
            discussion_stats={"status": "partial", "collected": 2, "limitations": ["采集受限"]},
        ),
    )
    plan.digest.merge_decision = merge_decision(
        plan.outcome, [], content_eligible=True, execution_complete=True,
    )
    text = "\n".join(discussion_report_lines(plan))
    assert "不完整" in text and "2 项讨论" in text and "采集受限" in text
    assert "复核完成" not in text and "已核验的既有风险" not in text
    for render in (render_summary, render_review_comment):
        assert "采集数量不代表已核验数量" in render(plan)
    plan.presentation = build_presentation(plan)
    assert render_summary(plan)
    assert render_review_comment(plan)
