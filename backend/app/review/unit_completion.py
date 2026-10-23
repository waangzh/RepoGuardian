"""Review Unit 完成与复用语义的领域级唯一来源。"""

from app.models.review import (
    IssueStatus,
    UnitDiffBatch,
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
# 这些尝试状态表示最近一次审查没有可用记录；聚合、严格完成与警告解释共用同一定义。
INCOMPLETE_ATTEMPT_STATUSES = frozenset({"missing", "invalid", "failed", "not_executed", "unknown"})


def is_review_unit_budget_exhausted(result: ReviewUnitResult) -> bool:
    return result.terminal_reason in BUDGET_EXHAUSTED_TERMINAL_REASONS


DIAGNOSIS_BACKGROUND_DEGRADED = "diagnosis_background_budget_degraded"


def batch_execution_status(batch: UnitDiffBatch) -> str:
    """返回批次执行状态；不把它当作目标覆盖结论。"""
    if batch.batch_execution_status != "pending":
        return batch.batch_execution_status
    if batch.status == "partial" and batch.reason == "review_step_no_evidence_progress":
        return "no_new_context"
    return batch.status


def review_unit_input_coverage(result: ReviewUnitResult) -> UnitInputCoverage | None:
    """仅识别明确的降级标记；旧记录和显式 legacy 的 unknown 不等于降级。"""
    if result.input_coverage is not None:
        return result.input_coverage
    summary = result.review_summary
    if (summary.status != "reported" or summary.record is None
            or summary.latest_attempt_status in INCOMPLETE_ATTEMPT_STATUSES):
        return UnitInputCoverage(reason=summary.latest_attempt_reason or summary.reason or "missing_review_record",
                                 evidence_coverage="unknown", target_coverage="unknown")
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
    summary = result.review_summary
    if (summary.status != "reported" or summary.record is None
            or summary.latest_attempt_status in {"missing", "invalid", "failed", "not_executed", "unknown"}):
        return False
    if result.diff_manifest is not None or result.coverage_ledger is not None:
        from app.services.unit_coverage import coverage_gaps
        active = result.active_evidence_set if result.diff_manifest and result.diff_manifest.review_unit_id != result.review_unit_id else None
        if coverage_gaps(result.diff_manifest, result.coverage_ledger, active, result.plan):
            return False
    coverage = review_unit_input_coverage(result)
    if coverage and coverage.contract_coverage != "complete":
        return False
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


def review_unit_record_gaps(result: ReviewUnitResult) -> list[str]:
    """自身或子批次最近一次审查尝试没有可用记录；与覆盖缺口分开解释。"""
    gaps: list[str] = []
    summary = result.review_summary
    if (summary.status != "reported" or summary.record is None
            or summary.latest_attempt_status in INCOMPLETE_ATTEMPT_STATUSES):
        gaps.append(f"{result.review_unit_id}:{summary.latest_attempt_status or summary.status}")
    for batch in result.diff_batches:
        if batch.result is not None:
            gaps.extend(review_unit_record_gaps(batch.result))
    return gaps


def review_unit_coverage_warning(result: ReviewUnitResult) -> str | None:
    if not is_review_unit_execution_complete(result) and result.input_coverage is None:
        return None
    coverage = review_unit_input_coverage(result)
    batch_states = [
        f"{batch.id}:{batch_execution_status(batch)}"
        for batch in result.diff_batches
        if batch_execution_status(batch) not in {"completed", "pending"}
    ]
    if (
        coverage is None
        or (
            coverage.target_coverage == "complete"
            and coverage.contract_coverage == "complete"
            and not batch_states
        )
    ):
        # 覆盖字段没有缺口时，严格完成仍可能被记录状态阻断；必须解释原因，
        # 不能同时给出“没有缺口”和“未完成”而不说明阻断来源。
        record_gaps = review_unit_record_gaps(result)
        return (
            f"Unit {result.review_unit_id}：覆盖字段无缺口，但审查记录不可用；"
            f"尝试状态 {'；'.join(record_gaps)}"
            if record_gaps
            else None
        )
    contract_detail = (
        f"；契约证据 {coverage.contract_evidence_coverage}"
        f"；契约核验 {coverage.contract_verification_status}"
    )
    blocking = (
        f"；阻塞依赖 {len(coverage.blocking_dependencies)} 项"
        if coverage.blocking_dependencies
        else ""
    )
    # 目标列表为空时不要暗示目标遗漏；契约缺口由契约核验字段表达。
    omitted = f"；省略目标：{'；'.join(coverage.omitted_targets)}" if coverage.omitted_targets else ""
    return (
        f"Unit {result.review_unit_id}：目标覆盖 {coverage.target_coverage}；"
        f"契约闭合 {coverage.contract_coverage}{contract_detail}{blocking}；"
        f"批次状态 {','.join(batch_states) or 'completed'}；"
        f"原因 {coverage.reason or 'diagnosis_input_incomplete'}{omitted}"
    )


def is_reusable_review_unit_result(result: ReviewUnitResult) -> bool:
    """跨任务复用比展示状态更保守，排除任何未确认结果。"""
    return (
        is_review_unit_complete(result)
        and result.review_summary.status == "reported"
        and result.review_summary.record is not None
        and result.review_summary.input_protocol == "canonical-evidence-v3"
        and result.review_summary.latest_attempt_status in (None, "reported")
        and result.human_request is None
        and not any(issue.status == IssueStatus.unresolved for issue in result.issues)
    )
