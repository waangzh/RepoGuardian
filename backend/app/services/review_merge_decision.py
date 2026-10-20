"""显式的报告建议策略；不授予 Approve 或合并权限。"""

from app.models.publication import MergeDecision
from app.services.publication_renderer import MERGE_LABELS, REASON_LABELS

_LIMITS = {
    "confirmed_findings_withheld": "仍有未通过发布准入的机器确认记录",
    "author_context_truncated": "作者声明输入已截断，未输入部分的目标尚未核验",
    "author_context_unknown": "作者声明输入是否完整未知，目标范围仍需确认",
    "finding_severity_unknown": "存在严重度未知的问题记录",
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
    # 作者声明对照是辅助分析：未逐项核验不构成合并阻断，明确矛盾只在评论风险中展示。
    if plan.digest.validation_state in {"failed", "unknown"}:
        unresolved.append("head_validation_" + plan.digest.validation_state)
    reasons = sorted(set(unresolved))
    decision = MergeDecision(blocking_finding_keys=blocking, nonblocking_finding_keys=suggestions,
                             unresolved_reasons=reasons,
                             limits=[_LIMITS.get(reason) or REASON_LABELS.get(reason, reason) for reason in reasons])
    if plan.outcome.kind in {"failed", "cancelled"} or not plan.content_eligible:
        decision.status = "unavailable"
        # 失败与用户停止的原因不同，标签必须让读者知道为什么没有结论。
        decision.label = {"failed": "审查因执行错误中断",
                          "cancelled": "审查已由用户停止"}.get(
                              plan.outcome.kind, MERGE_LABELS["unavailable"])
        return decision
    if blocking:
        decision.status = "fix_blockers"
        decision.advice = ("存在已核验且影响关键行为的问题，需要先处理。"
                           "请优先修复下列机器确认的 High/Critical 问题并在新快照重新核验；"
                           "人工标记已处理不能代替新快照核验。")
    elif reasons:
        decision.status = "resolve_unknowns"
        code_gaps = [reason for reason in reasons if reason not in _BOUNDARY_REASONS
                     and not reason.startswith(("head_validation_", "author_context_"))]
        decision.advice = ("当前审查存在足以影响决策的证据缺口；请先补齐下列事项。"
                           if code_gaps else
                           "当前审查的代码结论没有新增阻断项，但执行范围或验证记录仍有限制；补齐后再决定是否合并。")
    elif suggestions:
        # 只有非阻断建议时不能与“无问题”共用同一句结论，否则读者无法区分是否还有事项要处理。
        decision.status = "conditional_merge"
        decision.advice = ("在已检查范围内，本次静态审查没有发现需要阻止合并的问题；请结合 CI、审批和人工判断决定是否合并。"
                           f"另有 {len(suggestions)} 项已确认的非阻断问题，建议评估影响后处理。")
    elif plan.outcome.kind == "clean":
        decision.status = "conditional_merge"
        decision.advice = "在已检查范围内，本次静态审查没有发现需要阻止合并的问题；请结合 CI、审批和人工判断决定是否合并。"
    decision.label = (MERGE_LABELS["conditional_merge_with_suggestions"]
                      if decision.status == "conditional_merge" and suggestions
                      else MERGE_LABELS[decision.status])
    return decision
