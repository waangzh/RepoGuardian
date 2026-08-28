"""Review Unit 完成与复用语义的领域级唯一来源。"""

from app.models.review import (
    IssueStatus,
    ReviewUnitResult,
    ReviewUnitStatus,
    ReviewUnitTerminalReason,
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


def is_review_unit_complete(result: ReviewUnitResult) -> bool:
    """兼容旧 snapshot；未知或非成功终止原因默认不视为完整完成。"""
    return (
        result.status == ReviewUnitStatus.completed
        and result.terminal_reason in _COMPLETE_TERMINAL_REASONS
    )


def is_reusable_review_unit_result(result: ReviewUnitResult) -> bool:
    """跨任务复用比展示状态更保守，排除任何人工待确认结果。"""
    return (
        is_review_unit_complete(result)
        and result.human_request is None
        and not any(issue.status == IssueStatus.needs_human for issue in result.issues)
    )
