"""显式的报告建议策略；不授予 Approve 或合并权限。"""

from app.models.publication import MergeDecision
from app.services.publication_renderer import MERGE_LABELS, REASON_LABELS, review_limitation_kind, review_limitation_label

_LIMITS = {
    "confirmed_findings_withheld": "仍有未通过发布准入的机器确认记录",
    "author_context_truncated": "作者声明输入已截断，未输入部分的目标尚未核验",
    "author_context_unknown": "作者声明输入是否完整未知，目标范围仍需确认",
    "finding_severity_unknown": "存在严重度未知的问题记录",
    "head_validation_failed": "当前 Head 的已有验证记录为失败",
    "head_validation_unknown": "已有验证记录无法确定当前 Head 结果",
}


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
        decision.label = {"failed": "审查因执行错误中断，已保留部分记录",
                          "cancelled": "审查已停止，已保留停止前的记录"}.get(
                              plan.outcome.kind, MERGE_LABELS["unavailable"])
        return decision
    if blocking:
        decision.status = "fix_blockers"
        decision.advice = ("存在已核验且影响关键行为的问题，需要先处理。"
                           "请优先修复下列机器确认的 High/Critical 问题并在新快照重新核验；"
                           "人工标记已处理不能代替新快照核验。")
        if "head_validation_failed" in reasons:
            # 同时存在阻断与验证失败时不能只保留一个标题，否则概览与评论的主结论会不一致。
            decision.advice += " 另需核对当前 Head 的验证失败记录：静态问题修复不代表动态检查已恢复通过。"
            decision.label = f"发现 {len(blocking)} 项高优先级问题，另有当前版本验证失败"
            return decision
    elif reasons:
        decision.status = "resolve_unknowns"
        if "historical_discussion_unresolved" in reasons:
            # 历史复核只在取得独立代码证据时进入这里；这是已核验的风险，
            # 不是“未核实事项”，措辞必须与证据缺口区分。
            decision.label = "历史讨论中的问题经代码核验仍存在，需先核对"
            decision.advice = ("历史复核确认部分既有问题在当前固定代码中仍然成立；"
                "它们不属于本轮 Diff 新增问题，但会影响合并判断。请核对证据、影响范围与修复状态后再决定是否合并。")
            return decision
        if "head_validation_failed" in reasons:
            decision.label = "当前版本存在验证失败记录，需检查原因"
            decision.advice = "当前 Head 的已记录受控检查失败，请核对验证记录中的范围、来源与失败原因；不能将部分检查当作整个项目 CI。"
            return decision
        limitation = review_limitation_kind(plan.outcome, reasons)
        decision.label = review_limitation_label(plan.outcome, reasons)
        if limitation == "scope_incomplete":
            if not plan.outcome.total_goals:
                decision.label = "检查范围尚不明确"
                decision.advice = "没有可用的约定目标记录，无法判断检查范围；请先补齐范围或重新审查。"
            else:
                decision.advice = (f"约定目标已检查 {plan.outcome.examined_goals}/{plan.outcome.total_goals} 项；"
                    "请先核对并补查未完成范围，再决定是否合并。")
        elif limitation == "evidence_gaps":
            decision.advice = "仍有候选、关联调查或发布证据需要核对；请先核对限制说明并补齐代码或目标依据。"
        elif limitation == "runtime_limits":
            decision.advice = (f"约定静态目标已检查 {plan.outcome.examined_goals}/{plan.outcome.total_goals} 项；"
                "执行、计量或验证记录仍有限制，请分别核对检查范围与运行记录。"
                "运行或计量异常本身不代表发现代码缺陷，未知用量不能当作完整统计。")
        else:
            decision.advice = "尚无足够记录说明限制原因，请核对详细报告；不能据此判断代码存在缺陷或审查已完成。"
        return decision
    elif suggestions:
        # 只有非阻断建议时不能与“无问题”共用同一句结论，否则读者无法区分是否还有事项要处理。
        decision.status = "conditional_merge"
        decision.advice = ("在已检查范围内，本次静态审查没有发现需要阻止合并的问题；请结合 CI、审批和人工判断决定是否合并。"
                           f"另有 {len(suggestions)} 项已确认的非阻断问题，建议评估影响后处理。")
    elif plan.outcome.kind == "clean":
        decision.status = "conditional_merge"
        decision.advice = "在已检查范围内，本次静态审查没有发现需要阻止合并的问题；请结合 CI、审批和人工判断决定是否合并。"
    decision.label = (f"发现 {len(blocking)} 项高优先级问题，建议修复后再合并" if blocking
                      else f"发现 {len(suggestions)} 项改进建议，未发现阻断性问题" if suggestions
                      else MERGE_LABELS[decision.status])
    return decision
