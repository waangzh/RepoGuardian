"""显式的报告建议策略；不授予 Approve 或合并权限。"""

from app.models.publication import MergeDecision
from app.services.publication_renderer import REASON_LABELS

_LABELS = {
    "fix_blockers": "建议修改后再合并",
    "resolve_unknowns": "需要补充检查",
    "review_suggestions": "建议评估后合并",
    "conditional_merge": "未发现阻断性问题",
    "unavailable": "需要补充检查",
}
_LIMITS = {
    "confirmed_findings_withheld": "仍有未通过发布准入的机器确认记录",
    "author_context_truncated": "作者声明输入已截断，未输入部分的目标尚未核验",
    "author_context_unknown": "作者声明输入是否完整未知，目标范围仍需确认",
    "finding_severity_unknown": "存在严重度未知的问题记录",
    "author_intent_unresolved": "作者目标仍有未核验路径或矛盾",
    "head_validation_failed": "当前 Head 的已有验证记录为失败",
    "head_validation_unknown": "已有验证记录无法确定当前 Head 结果",
}
# 这些原因描述的是执行与审查边界，而不是代码本身存在缺陷。
_BOUNDARY_REASONS = {"budget_exhausted", "deadline_exceeded", "usage_integrity_unknown",
                     "termination_incomplete", "runtime_report_missing", "runtime_report_invalid"}


def merge_decision(plan):
    views = [item for item in plan.digest.findings if item.review_status == "confirmed"]
    blocking = sorted({item.id for item in views if item.severity in {"critical", "high"}})
    suggestions = sorted({item.id for item in views if item.severity in {"medium", "low"}})
    unresolved = [reason.value for reason in plan.outcome.reason_codes]
    if plan.digest.author_context_status in {"truncated", "unknown"}:
        unresolved.append("author_context_" + plan.digest.author_context_status)
    if plan.outcome.confirmed_count > plan.outcome.publishable_count:
        unresolved.append("confirmed_findings_withheld")
    if any(item.severity not in {"critical", "high", "medium", "low"} for item in views):
        unresolved.append("finding_severity_unknown")
    if any(item.status in {"contradicted", "insufficient", "not_checked"} for item in plan.digest.intent_assessments):
        unresolved.append("author_intent_unresolved")
    if plan.digest.validation_state in {"failed", "unknown"}:
        unresolved.append("head_validation_" + plan.digest.validation_state)
    reasons = sorted(set(unresolved))
    decision = MergeDecision(blocking_finding_keys=blocking, nonblocking_finding_keys=suggestions,
                             unresolved_reasons=reasons,
                             limits=[_LIMITS.get(reason) or REASON_LABELS.get(reason, reason) for reason in reasons])
    if plan.outcome.kind in {"failed", "cancelled"} or not plan.content_eligible:
        decision.label = _LABELS["unavailable"]
        return decision
    if blocking:
        decision.status = "fix_blockers"
        decision.advice = "优先修复下列机器确认的 High/Critical 问题并重新核验，再考虑合并；人工标记已处理不能代替新快照核验。"
    elif reasons:
        decision.status = "resolve_unknowns"
        code_gaps = [reason for reason in reasons if reason not in _BOUNDARY_REASONS
                     and not reason.startswith(("head_validation_", "author_context_"))]
        decision.advice = ("当前审查存在足以影响合并判断的证据缺口；请先补齐下列事项。"
                           if code_gaps else
                           "当前审查的代码结论没有新增阻断项，但执行范围或验证记录仍有限制；补齐后再决定是否合并。")
    elif suggestions or plan.outcome.confirmed_count:
        decision.status = "review_suggestions"
        decision.advice = "本轮已确认问题按当前严重度策略归为非阻断建议，需逐项评估影响与处理方式；确认相关测试、CI 和必要审批后再决定合并。"
    elif plan.outcome.kind == "clean":
        decision.status = "conditional_merge"
        decision.advice = "本次静态审查没有发现需要阻止合并的问题；请结合 CI、审批和人工判断决定是否合并。"
    decision.label = _LABELS[decision.status]
    return decision
