"""Review Unit 完成与复用语义的领域级唯一来源。"""

from app.models.review import (
    IssueStatus,
    ReviewUnitResult,
    ReviewUnitStatus,
    ReviewUnitTerminalReason,
    UnitInputCoverage,
)


BUDGET_EXHAUSTED_TERMINAL_REASONS = frozenset({
    ReviewUnitTerminalReason.model_budget_exhausted,
    ReviewUnitTerminalReason.retrieval_budget_exhausted,
    ReviewUnitTerminalReason.diagnosis_budget_exhausted,
})
_COMPLETE_TERMINAL_REASONS = frozenset({
    None,
    ReviewUnitTerminalReason.completed,
    ReviewUnitTerminalReason.no_issue,
    ReviewUnitTerminalReason.no_new_context,
})


def is_review_unit_budget_exhausted(result: ReviewUnitResult) -> bool:
    return result.terminal_reason in BUDGET_EXHAUSTED_TERMINAL_REASONS


DIAGNOSIS_BACKGROUND_DEGRADED = "diagnosis_background_budget_degraded"


def review_unit_input_coverage(result: ReviewUnitResult) -> UnitInputCoverage | None:
    """仅识别明确的降级标记；旧记录和显式 legacy 的 unknown 不等于降级。"""
    if result.input_coverage is not None:
        return result.input_coverage
    if DIAGNOSIS_BACKGROUND_DEGRADED in (
        result.plan_skip_reason, result.review_summary.reason,
        result.review_summary.latest_attempt_reason,
    ):
        return UnitInputCoverage(reason=DIAGNOSIS_BACKGROUND_DEGRADED,
            omitted_components=["unit_plan", "working_memory"])
    return None


def is_review_unit_execution_complete(result: ReviewUnitResult) -> bool:
    """执行完成允许收集候选；覆盖缺口不能把这些候选丢弃。"""
    return (
        result.status == ReviewUnitStatus.completed
        and result.terminal_reason in _COMPLETE_TERMINAL_REASONS
    )


def is_review_unit_complete(result: ReviewUnitResult) -> bool:
    if result.diff_manifest is not None or result.coverage_ledger is not None:
        from app.services.unit_coverage import coverage_gaps
        active = result.active_evidence_set if result.diff_manifest and result.diff_manifest.review_unit_id != result.review_unit_id else None
        if coverage_gaps(result.diff_manifest, result.coverage_ledger, active, result.plan):
            return False
    coverage = review_unit_input_coverage(result)
    if result.diff_batches and not all(
        batch.status == "completed" and batch.parent_unit_id == result.review_unit_id
        and batch.result is not None and batch.result.review_unit_id == batch.id
        and is_review_unit_complete(batch.result) and batch.result.review_summary.status == "reported"
        for batch in result.diff_batches
    ):
        return False
    return is_review_unit_execution_complete(result) and (
        coverage is None or coverage.target_coverage == "complete"
    )


def review_unit_coverage_warning(result: ReviewUnitResult) -> str | None:
    coverage = review_unit_input_coverage(result)
    if coverage is None or coverage.target_coverage == "complete":
        return None
    targets = "；".join(coverage.omitted_targets) or "未记录具体省略目标"
    return (f"Unit {result.review_unit_id} 目标覆盖 {coverage.target_coverage}："
            f"{coverage.reason or 'diagnosis_input_incomplete'}；省略目标：{targets}")


def is_reusable_review_unit_result(result: ReviewUnitResult) -> bool:
    """跨任务复用比展示状态更保守，排除任何人工待确认结果。"""
    return (
        is_review_unit_complete(result)
        and result.review_summary.status == "reported"
        and result.review_summary.record is not None
        and result.review_summary.input_protocol == "canonical-evidence-v3"
        and result.review_summary.latest_attempt_status in (None, "reported")
        and result.human_request is None
        and not any(issue.status == IssueStatus.needs_human for issue in result.issues)
    )
