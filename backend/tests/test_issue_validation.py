from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.models.review import (
    DeterministicIssueCheck,
    IssueDeduplicationDecision,
    IssueMetrics,
    IssueStatus,
    IssueVerification,
    ReviewIssue,
    ReviewUnit,
    ReviewUnitComplexity,
    Severity,
)
from app.services.issue_deduplication import IssueDeduplicationService
from app.services.issue_policy import IssuePolicyService, SeverityPolicy
from app.services.issue_verifier import IssueVerifierService
from app.services.review_rebuild import rebuild_task_from_state


def _unit(unit_id: str = "unit-1", path: str = "app.py") -> ReviewUnit:
    return ReviewUnit(
        id=unit_id,
        primary_files=[path],
        changed_symbols=["target"],
        rule_ids=["review.general"],
        estimated_tokens=100,
        complexity=ReviewUnitComplexity.small,
        fingerprint=unit_id,
        grouping_reason="test",
    )


def _issue(
    issue_id: str,
    *,
    unit_id: str = "unit-1",
    path: str = "app.py",
    anchor_hash: str = "anchor-1",
    severity: str = "medium",
    status: str = "evidence_resolved",
    symbol: str | None = "target",
    supporting: list[dict[str, Any]] | None = None,
) -> ReviewIssue:
    return ReviewIssue(
        id=issue_id,
        review_unit_id=unit_id,
        title="返回错误结果",
        category="correctness",
        severity=severity,
        confidence=0.9,
        affected_behavior="目标调用返回错误结果",
        failure_scenario="给定有效输入时稳定返回错误值",
        recommendation="修复条件判断",
        primary_evidence={
            "file_path": path,
            "existing_code": "return wrong",
            "symbol": symbol,
            "resolved_start_line": 10,
            "resolved_end_line": 10,
            "resolution_method": "diff_exact",
            "match_count": 1,
            "anchor_hash": anchor_hash,
            "resolved_side": "head",
            "candidate_locations": [{
                "file_path": path,
                "side": "head",
                "start_line": 10,
                "end_line": 10,
            }],
        },
        supporting_evidence=supporting or [],
        status=status,
        placement="inline",
    )


def _state(*units: ReviewUnit) -> dict[str, Any]:
    return {
        "model": None,
        "changed_files": [
            {
                "file_path": unit.primary_files[0],
                "change_type": "modified",
                "additions": 1,
                "deletions": 1,
                "hunks": [],
            }
            for unit in units
        ],
        "context_snippets": [],
    }


class VerifierProvider:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def verify_issue(self, request: Any, model: str | None) -> IssueVerification:
        del model
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            response = response(request)
        return IssueVerification.model_validate(response)

    async def deduplicate_issues(
        self, issues: list[ReviewIssue], model: str | None
    ) -> IssueDeduplicationDecision:
        del issues, model
        raise RuntimeError("semantic dedup should not be needed")


def test_missing_primary_evidence_is_deterministically_filtered() -> None:
    check = IssuePolicyService().check({
        "id": "invalid",
        "review_unit_id": "unit-1",
        "title": "问题",
        "category": "correctness",
        "severity": "medium",
        "confidence": 0.8,
        "affected_behavior": "行为错误",
        "failure_scenario": "输入会失败",
        "recommendation": "修复它",
    }, [_unit()])

    assert isinstance(check, DeterministicIssueCheck)
    assert check.passed is False
    assert check.reasons[0].startswith("schema_invalid")


def test_unresolved_anchor_never_passes_policy_or_becomes_confirmed() -> None:
    raw = _issue("unresolved").model_dump(mode="json")
    raw.update({
        "primary_evidence": {
            "file_path": "app.py",
            "existing_code": "missing",
            "resolution_method": "unresolved",
            "unresolved_reason": "code_not_found",
        },
        "status": IssueStatus.candidate,
    })
    issue = ReviewIssue.model_validate(raw)
    check = IssuePolicyService().check(issue, [_unit()])

    assert check.passed is False
    assert "primary_evidence_not_uniquely_resolved" in check.reasons


def test_high_severity_without_high_impact_evidence_is_stably_downgraded() -> None:
    issue = _issue("high", severity="high")
    policy = SeverityPolicy()

    assert policy.normalize(issue) == Severity.medium
    assert policy.normalize(issue) == Severity.medium


def test_verifier_decision_schema_only_allows_three_actions_and_no_new_issue() -> None:
    with pytest.raises(ValidationError):
        IssueVerification.model_validate({
            "issue_id": "x", "decision": "create", "reason": "新增问题"
        })
    with pytest.raises(ValidationError):
        IssueVerification.model_validate({
            "issue_id": "x",
            "decision": "keep",
            "reason": "保留",
            "new_issue": {"id": "forbidden"},
        })


@pytest.mark.asyncio
async def test_verifier_failure_uses_configured_mode_and_other_issue_continues() -> None:
    unit = _unit()
    issues = [_issue("first"), _issue("second", anchor_hash="anchor-2")]
    provider = VerifierProvider([
        RuntimeError("provider unavailable"),
        {"issue_id": "second", "decision": "keep", "reason": "证据可推出"},
    ])
    service = IssueVerifierService(
        provider,  # type: ignore[arg-type]
        enabled=True,
        fail_mode="needs_human",
        max_calls_per_unit=5,
    )
    result = await service.verify_issues(issues, [unit], _state(unit), IssueMetrics())

    assert [issue.status for issue in result.issues] == [
        IssueStatus.needs_human, IssueStatus.confirmed
    ]
    assert result.metrics.verifier_call_count == 2
    assert result.warnings


@pytest.mark.asyncio
async def test_verifier_calls_are_bounded_per_unit() -> None:
    unit = _unit()
    issues = [_issue("one"), _issue("two", anchor_hash="anchor-2")]
    provider = VerifierProvider([
        {"issue_id": "one", "decision": "keep", "reason": "成立"},
    ])
    service = IssueVerifierService(
        provider,  # type: ignore[arg-type]
        enabled=True,
        fail_mode="candidate",
        max_calls_per_unit=1,
    )
    result = await service.verify_issues(issues, [unit], _state(unit), IssueMetrics())

    assert provider.calls == 1
    assert result.issues[0].status == IssueStatus.confirmed
    assert result.issues[1].status == IssueStatus.candidate


@pytest.mark.asyncio
async def test_same_anchor_and_category_are_merged_with_evidence_and_sources() -> None:
    supporting = [{
        "file_path": "support.py",
        "existing_code": "proof",
        "resolved_start_line": 2,
        "resolved_end_line": 2,
        "resolution_method": "file_exact",
        "match_count": 1,
        "anchor_hash": "support-hash",
        "resolved_side": "head",
    }]
    first = _issue("first", unit_id="unit-1")
    second = _issue("second", unit_id="unit-2", supporting=supporting)
    provider = VerifierProvider([])
    result = await IssueDeduplicationService().aggregate(
        [first.model_copy(update={"status": IssueStatus.confirmed}),
         second.model_copy(update={"status": IssueStatus.confirmed})],
        provider,  # type: ignore[arg-type]
        None,
        IssueMetrics(),
    )

    assert len(result.issues) == 1
    assert result.metrics.duplicate_count == 1
    assert result.issues[0].supporting_evidence[0].anchor_hash == "support-hash"
    assert result.issues[0].source_review_unit_ids == ["unit-1", "unit-2"]
    assert result.issues[0].source_issue_ids == ["first", "second"]


@pytest.mark.asyncio
async def test_similar_text_with_different_anchor_is_not_merged_without_semantic_confirmation() -> None:
    issues = [
        _issue("first", anchor_hash="a").model_copy(update={"status": IssueStatus.confirmed}),
        _issue("second", anchor_hash="b").model_copy(update={"status": IssueStatus.confirmed}),
    ]
    result = await IssueDeduplicationService().aggregate(
        issues, VerifierProvider([]), None, IssueMetrics()  # type: ignore[arg-type]
    )

    assert len(result.issues) == 2
    assert result.metrics.duplicate_count == 0


@pytest.mark.asyncio
async def test_zero_confirmed_is_valid_and_metrics_are_accurate() -> None:
    issue = _issue("drop")
    provider = VerifierProvider([
        {"issue_id": "drop", "decision": "drop", "reason": "存在反例"},
    ])
    verified = await IssueVerifierService(
        provider,  # type: ignore[arg-type]
        enabled=True,
        fail_mode="needs_human",
        max_calls_per_unit=5,
    ).verify_issues([issue], [_unit()], _state(_unit()), IssueMetrics(candidate_issue_count=1))
    final = await IssueDeduplicationService().aggregate(
        verified.issues, provider, None, verified.metrics  # type: ignore[arg-type]
    )

    assert final.issues == []
    assert final.metrics.candidate_issue_count == 1
    assert final.metrics.verifier_drop_count == 1
    assert final.metrics.confirmed_count == 0
    assert final.metrics.needs_human_count == 0
    assert final.metrics.verifier_call_count == 1
    assert final.metrics.verifier_token_count > 0


def test_api_rebuild_only_publishes_confirmed_or_needs_human() -> None:
    candidates = [
        _issue("candidate", status="candidate"),
        _issue("confirmed", anchor_hash="b", status="confirmed"),
        _issue("human", anchor_hash="c", status="needs_human"),
        _issue("dismissed", anchor_hash="d", status="dismissed"),
    ]
    task = rebuild_task_from_state({
        "task_id": "task",
        "status": "completed",
        "phase": "completed",
        "review_issues": [issue.model_dump(mode="json") for issue in candidates],
    })

    assert [issue.id for issue in task.issues] == ["confirmed", "human"]
