"""显式的报告建议策略；不授予 Approve 或合并权限。"""

from app.models.publication import MergeDecision


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
    decision = MergeDecision(blocking_finding_keys=blocking, nonblocking_finding_keys=suggestions,
                             unresolved_reasons=sorted(set(unresolved)))
    if plan.outcome.kind in {"failed", "cancelled"} or not plan.content_eligible:
        return decision
    if blocking:
        decision.status = "fix_blockers"
        decision.advice = "优先修复以下机器确认的 High/Critical 阻断问题并重新核验，再考虑合并；人工标记已处理不能代替新快照核验。"
    elif unresolved:
        decision.status = "resolve_unknowns"
        decision.advice = "先补齐未决证据、被扣留的问题或验证限制，当前结果不足以支持合并判断。"
    elif suggestions or plan.outcome.confirmed_count:
        decision.status = "review_suggestions"
        decision.advice = "本轮已确认问题按当前严重度策略归为非阻断建议，需逐项评估影响与处理方式；确认相关测试、CI 和必要审批后再决定合并。"
    elif plan.outcome.kind == "clean":
        decision.status = "conditional_merge"
        decision.advice = "本轮静态审查未发现达到报告门槛的问题；确认相关测试、CI 与必要审批满足要求后，可以考虑进入合并流程。"
    return decision
