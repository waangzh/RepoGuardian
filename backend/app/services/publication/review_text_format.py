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
    "runtime_report_invalid": "任务运行报告格式无效",
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



def _comment_risks(plan):
    """只列当前审查限制；计量未知单独说明，不作为代码缺陷。"""
    risks = list(plan.digest.merge_decision.limits)
    if "usage_integrity_unknown" in plan.outcome.reason_codes:
        risks.append("运行计量记录不完整：模型请求计量不完整或不一致；未知用量不能当作完整统计。"
                     "运行或计量异常本身不代表发现代码缺陷。")
    return [f"- {value}" for value in dict.fromkeys(risks)]


# 只读审查的事实边界；所有报告正文共用同一措辞。
STATIC_BOUNDARY = "未执行目标仓库代码、测试或构建。"
