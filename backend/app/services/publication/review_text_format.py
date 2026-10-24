"""文本格式化与分类标签：策略与展示层共用的措辞，不依赖具体 renderer。"""

import html
import re
import unicodedata


OUTCOME_LABELS = {"clean": "本次检查未发现需在合并前处理的问题", "findings": "发现代码证据已确认的问题",
                  "inconclusive": "未得出完整结论", "failed": "审查中断，尚无完整结论",
                  "cancelled": "审查已停止，已保留部分结果"}


OUTCOME_DESCRIPTIONS = {
    "clean": "已完成本次约定范围的静态审查，在已检查范围内未发现具有充分代码证据、需要 PR 作者处理的问题。未发现问题不等于证明代码正确。",
    "findings": "请逐项核对下列代码证据已确认的问题及建议；仍需关注的限制见下方说明。",
    "inconclusive": "本轮变更范围内未新增代码证据已确认的问题，但审查仍存在缺口或可靠性限制，不能据此认定本次变更没有问题。",
    "failed": "审查因执行错误中断，请处理运行故障并重新审查；已有记录不能视为完整结论。",
    "cancelled": "审查已由用户停止，请补查剩余范围后再判断本次变更。",
}


REASON_LABELS = {
    "task_failed": "任务或运行时执行失败", "task_cancelled": "任务或运行时已取消",
    "termination_incomplete": "任务未可靠完成全部计划", "scope_incomplete": "部分目标未完成检查",
    "budget_exhausted": "审查预算耗尽，请核对是否仍有未完成目标",
    "deadline_exceeded": "审查达到时间上限，请核对是否仍有未完成目标或调查",
    "scope_empty": "没有实际审查目标", "verification_unresolved": "仍有未核实的候选问题",
    "finding_status_unknown": "存在未知的问题状态", "investigation_incomplete": "仍有未完成或证据不足的调查",
    "unscheduled_investigation": "仍有未处理的调查线索", "usage_integrity_unknown": "模型请求计量不完整或不一致",
    "snapshot_missing": "审查快照缺失或身份无效", "runtime_report_missing": "缺少任务运行报告",
    "runtime_report_invalid": "任务运行报告格式无效", "unsupported_legacy_projection": "历史引擎不支持此发布协议，请查看原始审计报告",
    "historical_discussion_unresolved": "历史讨论复核确认原问题在当前代码中仍存在，需核对",
}


MERGE_LABELS = {
    "fix_blockers": "发现高优先级问题，建议修复后复审",
    "resolve_unknowns": "存在未确认事项，需进一步核实",
    "conditional_merge": "本次检查未发现需在合并前处理的问题",
    "conditional_merge_with_suggestions": "有改进建议，暂无高优先级问题",
    "unavailable": "暂不能给出合并建议",
}


MERGE_SYMBOLS = {
    "fix_blockers": "⚠️",
    "resolve_unknowns": "❓",
    "conditional_merge": "✅",
    "unavailable": "❓",
}


INCOMPLETE_LABELS = {
    "scope_incomplete": "还有约定检查未完成",
    "evidence_gaps": MERGE_LABELS["resolve_unknowns"],
    "runtime_limits": "审查已结束，但结果可靠性仍需核对",
}


_EVIDENCE_REASONS = {
    "verification_unresolved", "finding_status_unknown", "investigation_incomplete",
    "unscheduled_investigation", "confirmed_findings_withheld", "finding_severity_unknown",
    "author_context_truncated", "author_context_unknown", "historical_discussion_unresolved",
}


_RUNTIME_REASONS = {
    "budget_exhausted", "deadline_exceeded", "usage_integrity_unknown", "termination_incomplete",
    "snapshot_missing", "runtime_report_missing", "runtime_report_invalid", "unsupported_legacy_projection",
    "head_validation_failed", "head_validation_unknown",
}


def review_limitation_kind(outcome, reasons=()):
    """仅按保存事实区分范围、代码证据与运行记录限制，不改变 Outcome。"""
    reasons = set(outcome.reason_codes) | set(reasons)
    if reasons & {"scope_incomplete", "scope_empty"} or (
            outcome.total_goals > 0 and outcome.examined_goals < outcome.total_goals):
        return "scope_incomplete"
    if outcome.unresolved_count or outcome.incomplete_investigations or reasons & _EVIDENCE_REASONS:
        return "evidence_gaps"
    if reasons & _RUNTIME_REASONS or outcome.runtime_reliability != "reliable":
        return "runtime_limits"
    return None


def review_limitation_label(outcome, reasons=()):
    reasons = set(outcome.reason_codes) | set(reasons)
    kind = review_limitation_kind(outcome, reasons)
    if kind == "scope_incomplete":
        if not outcome.total_goals:
            return "检查范围尚不明确"
        remaining = outcome.total_goals - outcome.examined_goals
        return f"还有 {remaining} 项约定检查未完成" if remaining > 0 else "尚无法确认检查范围是否完整"
    if kind == "evidence_gaps":
        if outcome.unresolved_count:
            return f"有 {outcome.unresolved_count} 项疑点需要进一步确认"
        if outcome.incomplete_investigations:
            return f"有 {outcome.incomplete_investigations} 项关联调查尚未完成"
        if reasons & {"author_context_truncated", "author_context_unknown"}:
            return "作者声明范围尚待核对"
        if "confirmed_findings_withheld" in reasons:
            return "部分代码证据已确认的记录未通过交付准入，需核对证据"
        return "存在未确认事项，需进一步核实"
    return INCOMPLETE_LABELS.get(kind, "审查结论暂不完整")


def markdown_text(value: str, limit: int = 2000) -> str:
    text = " ".join("".join(char for char in value if not unicodedata.category(char).startswith("C") or char in "\n\t").split())
    text = text[:limit] + ("…" if len(text) > limit else "")
    return re.sub(r"([\\`*_{}\[\]()#+.!|~>-])", r"\\\1", html.escape(text, quote=True)).replace("@", "&#64;")


def _sentence(value: str | None, limit: int = 240) -> str:
    """取一段可读文本的首句，避免评论被整段模型输出淹没。"""
    from app.services.publication.review_prose import concise_text
    return concise_text(value, limit)


def _ordered_findings(plan):
    rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    return sorted([*plan.inline_drafts, *plan.summary_findings], key=lambda item: (
        rank[item.severity], item.anchor.file_path, item.anchor.start_line or 0, item.finding_key))


def _finding_location_text(finding) -> str:
    anchor = finding.anchor
    location = markdown_text(anchor.file_path, 500)
    if anchor.start_line:
        location += f":{anchor.start_line}" + (
            f"–{anchor.end_line}" if anchor.end_line and anchor.end_line != anchor.start_line else "")
    if anchor.side:
        location += f"（{anchor.side}）"
    return location



def _comment_risks(plan, *, include_history=True, include_investigations=True):
    """只列影响合并判断的限制；未逐项核验的作者声明留在详细报告。

    已核验的历史风险在评论中有独立章节，通用限制行不再重复同一条结论；
    尚未完成的历史调查仍由待确认章节逐项呈现。
    """
    reasons = {item.value for item in plan.outcome.reason_codes}
    risks = [value for value in plan.digest.merge_decision.limits
             if (include_history or value != REASON_LABELS["historical_discussion_unresolved"])
             and (include_investigations or not (
                 "investigation_incomplete" in reasons
                 and value == REASON_LABELS["investigation_incomplete"]))]
    if "usage_integrity_unknown" in plan.outcome.reason_codes:
        risks.append("运行计量记录不完整：模型请求计量不完整或不一致；未知用量不能当作完整统计。"
                     "运行或计量异常本身不代表发现代码缺陷。")
    for item in plan.digest.intent_assessments:
        if item.status == "contradicted":
            locations = []
            for observation in item.observations:
                if observation.get("alignment") != "contradicted":
                    continue
                path = observation.get("path") or "未知文件"
                line = observation.get("start_line")
                locations.append(f"{path}:{line}" if line else path)
            suffix = "，位置：" + "、".join(dict.fromkeys(locations)) if locations else ""
            risks.append(f"作者声明“{_sentence(item.author_target, 120)}”与代码存在静态证据矛盾{suffix}。")
    pending = [item for item in plan.digest.investigations if item.answer == "unresolved"]
    if pending and include_investigations:
        risks.append(f"{len(pending)} 项调查未得到充分答案，相关调用链结论仍需补充证据。")
    if plan.digest.validation_state == "failed":
        risks.append(_validation_sentence(plan))
    return [f"- {value}" for value in dict.fromkeys(risks)]


def _validation_sentence(plan) -> str:
    state = plan.digest.validation_state
    return {
        "not_executed": ("当前 Head 未执行受控动态验证；Base 参考与独立补丁结果仅适用于各自对象。"
            if plan.digest.validations else "未执行目标仓库代码、测试或构建。"),
        "passed": "已有与当前 Head 绑定的受控验证通过记录；不代替完整 CI 或审批。",
        "failed": "已有与当前 Head 绑定的验证失败记录，需核对失败原因。",
        "independent_results": "已有独立补丁验证结果；补丁通过不代表当前 PR Head 已通过测试。",
        "unknown": "动态验证状态未知；不能据此宣称目标仓库测试或构建已通过。",
    }[state]
