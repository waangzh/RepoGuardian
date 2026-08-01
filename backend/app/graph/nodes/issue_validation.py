"""候选 Issue 的确定性策略、独立验证和最终去重节点。"""

from typing import Any

from app.core.config import settings
from app.graph.nodes._events import append_step
from app.graph.state import ReviewState
from app.models.review import (
    IssueMetrics,
    IssueStatus,
    ReviewIssue,
    ReviewUnit,
    ReviewUnitComplexity,
    ReviewUnitResult,
)
from app.services.issue_deduplication import IssueDeduplicationService
from app.services.issue_policy import IssuePolicyService
from app.services.issue_verifier import IssueVerifierService


async def issue_policy_node(state: ReviewState) -> ReviewState:
    service: Any = state.get("_issue_policy_service") or IssuePolicyService()
    units = [ReviewUnit.model_validate(item) for item in state.get("review_units") or []]
    issues = [ReviewIssue.model_validate(item) for item in state.get("review_issues") or []]
    if not units and issues:
        by_unit: dict[str, list[str]] = {}
        for issue in issues:
            by_unit.setdefault(issue.review_unit_id, []).append(issue.primary_evidence.file_path)
        units = [
            ReviewUnit(
                id=unit_id,
                primary_files=list(dict.fromkeys(paths)),
                estimated_tokens=0,
                complexity=ReviewUnitComplexity.small,
                fingerprint=f"legacy:{unit_id}",
                grouping_reason="legacy_review_path",
            )
            for unit_id, paths in by_unit.items()
        ]
    metrics = IssueMetrics(candidate_issue_count=len(issues))
    checks = []
    checked: list[ReviewIssue] = []
    deterministic_drops = 0
    severity_adjustments = 0

    for issue in issues:
        check = service.check(issue, units)
        checks.append(check)
        if check.passed:
            normalized = check.normalized_severity or issue.severity
            if normalized != issue.severity:
                severity_adjustments += 1
            updated = issue.model_copy(update={"severity": normalized})
            updated = updated.model_copy(update={
                "auto_fix_eligible": service.auto_fix_allowed(updated),
            })
        else:
            deterministic_drops += 1
            preserve_human = issue.status == IssueStatus.needs_human
            updated = issue.model_copy(update={
                "status": IssueStatus.needs_human if preserve_human else IssueStatus.dismissed,
                "requires_human_confirmation": preserve_human,
                "auto_fix_eligible": False,
                "unresolved_reason": issue.unresolved_reason or ";".join(check.reasons),
            })
        checked.append(updated)

    metrics = metrics.model_copy(update={
        "deterministic_drop_count": deterministic_drops,
        "severity_adjustment_count": severity_adjustments,
    })
    return ReviewState(
        status="verifying_issues",
        review_units=[unit.model_dump(mode="json") for unit in units],
        review_issues=[issue.model_dump(mode="json") for issue in checked],
        deterministic_issue_checks=[check.model_dump(mode="json") for check in checks],
        issue_metrics=metrics.model_dump(mode="json"),
        step_progress=append_step(
            state,
            "issue_policy",
            "completed",
            f"确定性检查 {len(issues)} 个候选，过滤 {deterministic_drops} 个",
        ),
    )


async def issue_verifier_node(state: ReviewState) -> ReviewState:
    issues = [ReviewIssue.model_validate(item) for item in state.get("review_issues") or []]
    units = [ReviewUnit.model_validate(item) for item in state.get("review_units") or []]
    metrics = IssueMetrics.model_validate(state.get("issue_metrics") or {})
    service: Any = state.get("_issue_verifier_service")
    if service is None:
        provider = state.get("_provider")
        if provider is None:
            raise ValueError("issue verifier requires an injected provider")
        service = IssueVerifierService(
            provider,
            enabled=settings.repoguardian_issue_verifier_enabled,
            fail_mode=settings.repoguardian_issue_verifier_fail_mode,
            max_calls_per_unit=settings.repoguardian_issue_verifier_max_calls_per_unit,
        )
    result = await service.verify_issues(issues, units, dict(state), metrics)
    warnings = list(dict.fromkeys([*(state.get("warnings") or []), *result.warnings]))
    verified_count = sum(issue.status == IssueStatus.confirmed for issue in result.issues)
    return ReviewState(
        status="verifying_issues",
        review_issues=[issue.model_dump(mode="json") for issue in result.issues],
        issue_verifications=[item.model_dump(mode="json") for item in result.verifications],
        issue_metrics=result.metrics.model_dump(mode="json"),
        warnings=warnings,
        step_progress=append_step(
            state,
            "issue_verifier",
            "completed",
            f"独立验证确认 {verified_count} 个 Issue，失败不会自动 keep",
        ),
    )


async def issue_deduplication_node(state: ReviewState) -> ReviewState:
    issues = [ReviewIssue.model_validate(item) for item in state.get("review_issues") or []]
    metrics = IssueMetrics.model_validate(state.get("issue_metrics") or {})
    provider = state.get("_provider")
    if provider is None:
        raise ValueError("issue deduplication requires an injected provider")
    service: Any = state.get("_issue_deduplication_service") or IssueDeduplicationService()
    result = await service.aggregate(issues, provider, state.get("model"), metrics)

    by_unit: dict[str, list[ReviewIssue]] = {}
    for issue in result.issues:
        by_unit.setdefault(issue.review_unit_id, []).append(issue)
    unit_results: list[dict] = []
    for raw in state.get("review_unit_results") or []:
        unit_result = ReviewUnitResult.model_validate(raw)
        unit_results.append(unit_result.model_copy(update={
            "issues": by_unit.get(unit_result.review_unit_id, []),
        }).model_dump(mode="json"))

    return ReviewState(
        status="verifying_issues",
        review_issues=[issue.model_dump(mode="json") for issue in result.issues],
        review_unit_results=unit_results,
        issue_deduplication_decisions=[
            item.model_dump(mode="json") for item in result.decisions
        ],
        issue_metrics=result.metrics.model_dump(mode="json"),
        step_progress=append_step(
            state,
            "issue_deduplication",
            "completed",
            f"最终发布 {len(result.issues)} 个 Issue，合并 {result.metrics.duplicate_count} 个重复项",
        ),
    )
