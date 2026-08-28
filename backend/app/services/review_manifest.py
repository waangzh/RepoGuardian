"""从 Review Plane 状态构建 coverage 与可审计 run manifest。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.core.config import settings
from app.models.review import (
    IssueStatus,
    ModelUsage,
    PlannedChangedFile,
    ReviewCoverage,
    ReviewFileCoverage,
    ReviewFileStatus,
    ReviewRunManifest,
    ReviewUnit,
    ReviewUnitCoverage,
    ReviewUnitResult,
    ReviewUnitStatus,
    ReviewUnitTerminalReason,
)


_BUDGET_TERMINAL_REASONS = {
    ReviewUnitTerminalReason.model_budget_exhausted,
    ReviewUnitTerminalReason.retrieval_budget_exhausted,
    ReviewUnitTerminalReason.diagnosis_budget_exhausted,
}


def build_review_manifest(state: dict[str, Any], completed_at: datetime) -> ReviewRunManifest:
    units = [ReviewUnit.model_validate(item) for item in state.get("review_units") or []]
    results = {
        item.review_unit_id: item
        for item in (
            ReviewUnitResult.model_validate(raw)
            for raw in state.get("review_unit_results") or []
        )
    }
    planned = [
        PlannedChangedFile.model_validate(item)
        for item in (state.get("review_plan") or {}).get("changed_files", [])
    ]
    owners: dict[str, list[str]] = {}
    for unit in units:
        for path in unit.primary_files:
            owners.setdefault(path, []).append(unit.id)

    files: list[ReviewFileCoverage] = []
    for item in planned:
        unit_ids = owners.get(item.file_path, [])
        unit_results = [results[unit_id] for unit_id in unit_ids if unit_id in results]
        if not item.included:
            status = (
                ReviewFileStatus.excluded_binary
                if item.excluded_reason == "binary_file"
                else ReviewFileStatus.excluded_generated
                if item.excluded_reason == "generated_file"
                else ReviewFileStatus.excluded_sensitive
                if item.excluded_reason == "sensitive_file"
                else ReviewFileStatus.unsupported
            )
            reason = item.excluded_reason
        elif (
            len(unit_results) == len(unit_ids)
            and unit_results
            and all(_is_fully_completed(result) for result in unit_results)
        ):
            status, reason = ReviewFileStatus.reviewed, None
        elif any(_is_fully_completed(result) for result in unit_results):
            status, reason = ReviewFileStatus.partial, _result_summary(
                unit_results, missing_units=len(unit_ids) - len(unit_results)
            )
        elif any(result.terminal_reason in _BUDGET_TERMINAL_REASONS for result in unit_results):
            status, reason = ReviewFileStatus.budget_exhausted, _result_summary(
                unit_results, missing_units=len(unit_ids) - len(unit_results)
            )
        elif unit_results and all(
            result.status == ReviewUnitStatus.timed_out for result in unit_results
        ):
            status, reason = ReviewFileStatus.timed_out, _result_summary(
                unit_results, missing_units=len(unit_ids) - len(unit_results)
            )
        elif unit_ids:
            status, reason = ReviewFileStatus.model_failed, _result_summary(
                unit_results, missing_units=len(unit_ids) - len(unit_results)
            )
        else:
            status, reason = ReviewFileStatus.unsupported, "no_review_unit"
        files.append(ReviewFileCoverage(
            file_path=item.file_path,
            eligible=item.included,
            status=status,
            review_unit_ids=unit_ids,
            reason=reason,
        ))

    unit_coverage: list[ReviewUnitCoverage] = []
    for unit in units:
        result = results.get(unit.id)
        usages = list(result.model_usages if result else [])
        unit_coverage.append(ReviewUnitCoverage(
            review_unit_id=unit.id,
            files=unit.primary_files,
            status=result.status if result else ReviewUnitStatus.pending,
            terminal_reason=result.terminal_reason if result else None,
            failure_reason=result.error if result else "unit_not_executed",
            model_calls=len(usages),
            tokens=sum(usage.actual_total_tokens or usage.accounted_tokens_estimate or 0 for usage in usages),
            duration_ms=sum(usage.latency_ms for usage in usages),
        ))

    eligible = sum(item.eligible for item in files)
    reviewed = sum(item.status == ReviewFileStatus.reviewed for item in files)
    partial = sum(item.status == ReviewFileStatus.partial for item in files)
    failed_statuses = {
        ReviewFileStatus.timed_out,
        ReviewFileStatus.model_failed,
        ReviewFileStatus.budget_exhausted,
    }
    failed = sum(item.status in failed_statuses for item in files)
    skipped_statuses = {
        ReviewFileStatus.excluded_binary,
        ReviewFileStatus.excluded_generated,
        ReviewFileStatus.excluded_sensitive,
        ReviewFileStatus.unsupported,
    }
    completed_units = sum(
        result is not None and _is_fully_completed(result)
        for result in (results.get(unit.id) for unit in units)
    )
    coverage = ReviewCoverage(
        changed_files=len(files),
        eligible_files=eligible,
        reviewed_files=reviewed,
        partial_files=partial,
        skipped_files=sum(item.status in skipped_statuses for item in files),
        failed_files=failed,
        coverage_rate=(reviewed / eligible if eligible else 1.0),
        completed_units=completed_units,
        total_units=len(units),
        unit_coverage_rate=(completed_units / len(units) if units else 1.0),
        files=files,
        units=unit_coverage,
    )

    usages = _all_usages(state)
    started_at = _as_datetime(state.get("created_at"), completed_at)
    pr = state.get("pr_info") or {}
    issues = state.get("review_issues") or []
    return ReviewRunManifest(
        review_id=str(state.get("task_id") or ""),
        repository=(f"{pr.get('owner')}/{pr.get('repo')}" if pr.get("owner") and pr.get("repo") else None),
        pr_number=pr.get("number") or None,
        base_sha=state.get("base_sha"),
        head_sha=state.get("head_sha"),
        planner_version=(state.get("review_plan") or {}).get("planner_version"),
        provider=settings.repoguardian_provider,
        model=str(state.get("model") or settings.repoguardian_model),
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=max(0, int((completed_at - started_at).total_seconds() * 1000)),
        model_calls=len(usages),
        input_tokens=sum(usage.actual_input_tokens or 0 for usage in usages),
        output_tokens=sum(usage.actual_output_tokens or 0 for usage in usages),
        total_tokens=sum(usage.actual_total_tokens or usage.accounted_tokens_estimate or 0 for usage in usages),
        confirmed_issues=sum(item.get("status") == IssueStatus.confirmed.value for item in issues),
        coverage=coverage,
        warnings=list(state.get("warnings") or []),
    )


def _all_usages(state: dict[str, Any]) -> list[ModelUsage]:
    raw = list(state.get("model_usages") or [])
    for result in state.get("review_unit_results") or []:
        raw.extend(result.get("model_usages") or [])
    by_id = {usage.id: usage for usage in (ModelUsage.model_validate(item) for item in raw)}
    return list(by_id.values())


def _is_fully_completed(result: ReviewUnitResult) -> bool:
    return (
        result.status == ReviewUnitStatus.completed
        and result.terminal_reason not in _BUDGET_TERMINAL_REASONS
    )


def _result_summary(
    results: list[ReviewUnitResult], *, missing_units: int = 0
) -> str:
    reasons = [
        result.error or (result.terminal_reason.value if result.terminal_reason else result.status.value)
        for result in results
    ]
    if missing_units:
        reasons.append(f"{missing_units} unit(s) not executed")
    return "; ".join(dict.fromkeys(reasons)) or "unit_not_executed"


def _as_datetime(value: Any, default: datetime) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return default
