"""显式的报告建议策略；不授予 Approve 或合并权限。"""

from app.models.publication import MergeDecision
from app.services.publication_renderer import MERGE_LABELS, REASON_LABELS, review_limitation_kind, review_limitation_label
from app.services.pr_discussion_matching import exact_duplicate_matches

_LIMITS = {
    "confirmed_findings_withheld": "仍有代码证据已确认但未通过发布准入的记录",
    "author_context_truncated": "作者声明输入已截断，未输入部分的目标尚未核验",
    "author_context_unknown": "作者声明输入是否完整未知，目标范围仍需确认",
    "finding_severity_unknown": "存在严重度未知的问题记录",
    "head_validation_failed": "当前 Head 的已有验证记录为失败",
    "head_validation_unknown": "已有验证记录无法确定当前 Head 结果",
}
# 合并建议是审查策略输出，不是 GitHub Mergeability 或分支保护结论。
POLICY_BOUNDARY = "这是 RepoGuardian 按审查策略给出的建议，不代表 GitHub 的 Mergeability 状态或分支保护会阻止合并。"


def _blocking_label(independent: int, current: int, historical: int, *, validation_failed: bool = False) -> str:
    """标题给出按缺陷身份去重后的数量，并保留两份证据来源，避免把同一缺陷读成两项。

    只有在独立数与来源数一致时才沿用原有短语；否则明确写出“含历史复核”
    并标注当前记录数，让读者知道总数不是简单相加。
    """
    base = f"发现 {independent} 项高优先级问题"
    if historical:
        if independent == current + historical:
            base += "（含历史复核）"
        else:
            base += f"（含 {historical} 项历史复核，{current} 项本轮记录）"
    return base + ("，另有当前版本动态检查未通过" if validation_failed else "，建议修复后复审")


def _history_advice(deduplicated: int, historical: int = 0) -> str:
    if not historical:
        return ""
    advice = " 历史复核问题应按其当前影响修复，不计入本轮 Diff 新增问题。"
    if deduplicated:
        advice += (f" 其中 {deduplicated} 项与本轮记录指向同一缺陷，已按一项计入标题，"
                   "证据来源仍分别保留在记录中。")
    return advice


def merge_decision(plan):
    views = [item for item in plan.digest.findings if item.review_status == "confirmed"]
    blocking = sorted({item.id for item in views if item.severity in {"critical", "high"}})
    suggestions = sorted({item.id for item in views if item.severity in {"medium", "low"}})
    # 计量未知保留审计含义，不作为代码核验或合并判断的证据缺口。
    unresolved = [reason.value for reason in plan.outcome.reason_codes if reason != "usage_integrity_unknown"]
    history = plan.outcome.historical_discussion
    history_blocking = sorted({item.discussion_id for item in history.findings if item.priority == "fix_before_merge"})
    history_suggestions = sorted({item.discussion_id for item in history.findings if item.priority == "assess_improvement"})
    classified_history = set(history_blocking + history_suggestions)
    # 同一缺陷的当前记录与历史复核只计一次；只有执行器保存的主体、触发条件、影响三项都相同，
    # 且没有新信息时才关联，文件路径或标题相似不足以去重。
    linked = {match.get("discussion_id") for finding in [*plan.inline_drafts, *plan.summary_findings]
              for match in exact_duplicate_matches(finding.discussion_matches)}
    deduplicated = sorted(identity for identity in linked & classified_history if identity)
    independent_blocking = len(blocking) + len(set(history_blocking) - linked)
    independent_suggestions = len(suggestions) + len(set(history_suggestions) - linked)
    if history.affects_risk and (not history.findings or any(
            item.get("discussion_id") not in classified_history for item in history.verified_defects)):
        unresolved.append("historical_discussion_unresolved")
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
    if history_blocking or history_suggestions:
        decision.blocking_historical_keys = history_blocking
        decision.nonblocking_historical_keys = history_suggestions
        # 标题面向 PR 作者按独立缺陷计数；键列表继续保留两份证据来源供审计核对。
        decision.blocking_independent_count = independent_blocking
        decision.nonblocking_independent_count = independent_suggestions
        decision.deduplicated_historical_keys = deduplicated
    if plan.outcome.kind in {"failed", "cancelled"} or not plan.content_eligible:
        decision.status = "unavailable"
        # 失败与用户停止的原因不同，标签必须让读者知道为什么没有结论。
        decision.label = {"failed": "审查中断，尚无完整结论",
                          "cancelled": "审查已停止，已保留部分结果"}.get(
                              plan.outcome.kind, MERGE_LABELS["unavailable"])
        return decision
    if blocking or history_blocking:
        decision.status = "fix_blockers"
        decision.advice = ("存在有代码证据支持、按审查策略建议合并前处理的高优先级问题。"
                           "请优先修复下列问题并在新快照重新核验；人工标记已处理不能代替新快照核验。"
                           + POLICY_BOUNDARY)
        if history_blocking:
            decision.label = _blocking_label(independent_blocking, len(blocking), len(history_blocking))
            decision.advice += _history_advice(len(deduplicated), len(history_blocking))
            if "head_validation_failed" not in reasons:
                return decision
        if "head_validation_failed" in reasons:
            # 同时存在高优先级问题与验证失败时不能只保留一个标题，否则概览与评论的主结论会不一致。
            decision.advice += " 另需核对当前 Head 的动态检查失败记录：静态问题修复不代表动态检查已恢复通过。"
            decision.label = _blocking_label(independent_blocking, len(blocking), len(history_blocking),
                                             validation_failed=True)
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
            decision.label = "本次记录的动态检查未通过"
            decision.advice = "本次记录的动态检查未通过，请核对验证记录中的范围、来源与失败原因；不能将部分检查当作整个项目 CI。"
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
    elif suggestions or history_suggestions:
        # 只有非高优先级建议时不能与“无问题”共用同一句结论，否则读者无法区分是否还有事项要处理。
        decision.status = "conditional_merge"
        decision.advice = ("在已检查范围内，本次静态审查没有发现按审查策略建议合并前处理的问题；请结合 CI、审批和人工判断决定是否合并。"
                           f"另有 {independent_suggestions} 项代码证据已确认的改进建议，建议评估影响后处理。"
                           + _history_advice(len(deduplicated), len(history_suggestions)) + POLICY_BOUNDARY)
    elif plan.outcome.kind == "clean" or (plan.outcome.review_completeness == "complete"
            and set(plan.outcome.reason_codes) == {"usage_integrity_unknown"}):
        decision.status = "conditional_merge"
        decision.advice = "在已检查范围内，本次静态审查没有发现按审查策略建议合并前处理的问题；请结合 CI、审批和人工判断决定是否合并。"
        if "usage_integrity_unknown" in plan.outcome.reason_codes:
            decision.label = "已检查范围内未发现已确认问题"
            decision.advice += " 运行计量记录不完整，未知用量不能当作完整统计；运行或计量异常本身不代表发现代码缺陷。"
            return decision
    decision.label = (f"发现 {len(blocking)} 项高优先级问题，建议修复后复审" if blocking
                      else f"发现 {independent_suggestions} 项改进建议，暂无高优先级问题" if suggestions or history_suggestions
                      else MERGE_LABELS[decision.status])
    return decision
