"""仅渲染后端已准入文本；不访问 GitHub、模型或仓库。"""

import html
import re
import unicodedata

from app.models.publication import PublicationFinding, PublicationPlan


OUTCOME_LABELS = {"clean": "未发现可行动问题", "findings": "发现已确认问题", "inconclusive": "本次审查未得出完整结论",
                  "failed": "审查失败", "cancelled": "审查已取消"}
REASON_LABELS = {
    "task_failed": "任务或运行时执行失败", "task_cancelled": "任务或运行时已取消",
    "termination_incomplete": "任务未可靠完成全部计划", "scope_incomplete": "部分目标未完成检查",
    "budget_exhausted": "审查预算耗尽，请补查未完成目标后再判断本次变更",
    "deadline_exceeded": "审查达到时间上限，请补查未完成目标或调查",
    "scope_empty": "没有实际审查目标", "verification_unresolved": "仍有未核实的候选问题",
    "finding_status_unknown": "存在未知的问题状态", "investigation_incomplete": "仍有未完成或证据不足的调查",
    "unscheduled_investigation": "仍有未处理的调查线索", "usage_integrity_unknown": "模型请求计量不完整或不一致",
    "snapshot_missing": "审查快照缺失或身份无效", "runtime_report_missing": "缺少任务运行报告",
    "runtime_report_invalid": "任务运行报告格式无效", "unsupported_legacy_projection": "历史引擎不支持此发布协议，请查看原始审计报告",
}


def markdown_text(value: str, limit: int = 2000) -> str:
    text = " ".join("".join(char for char in value if not unicodedata.category(char).startswith("C") or char in "\n\t").split())
    text = text[:limit] + ("…" if len(text) > limit else "")
    return re.sub(r"([\\`*_{}\[\]()#+.!|~>-])", r"\\\1", html.escape(text, quote=True)).replace("@", "&#64;")


def render_finding(finding: PublicationFinding) -> str:
    """正文已由 Planner 从 verified publication 文本生成，不再回退候选。"""
    return finding.body_markdown


def finding_body(text: dict, severity: str, head_sha: str) -> str:
    return (f"**[{severity.title()}] {markdown_text(text['claim'], 200)}**\n\n"
            f"**影响：** {markdown_text(text['affected_behavior'])}\n\n"
            f"**触发场景：** {markdown_text(text['failure_scenario'])}\n\n"
            f"**建议：** {markdown_text(text['recommendation'])}\n\n"
            f"审查 Commit：{markdown_text(head_sha, 64)}。静态审查结论。")


def render_summary(plan: PublicationPlan) -> str:
    outcome = plan.outcome
    digest = plan.digest
    descriptions = {
        "clean": "已完成本次约定范围的静态审查，未发现具有充分代码证据、需要 PR 作者处理的问题。",
        "findings": "请逐项核对下列已确认问题及建议；仍需关注的限制见下方说明。",
        "inconclusive": "目前没有可确认的问题，但审查仍存在缺口或可靠性限制，不能据此认定本次变更没有问题。",
        "failed": "审查执行失败，请处理运行故障并重新审查；已有记录不能视为完整结论。",
        "cancelled": "审查已取消，请补查剩余范围后再判断本次变更。",
    }
    lines = ["## RepoGuardian · PR Review", "", f"**{OUTCOME_LABELS[outcome.kind]}**", "",
             descriptions[outcome.kind], "", f"- 已确认问题：{outcome.confirmed_count}",
             f"- 审查目标完成：{outcome.examined_goals} / {outcome.total_goals}",
             f"- 未决候选：{outcome.unresolved_count}", "", "### 审查范围", ""]
    area_labels = {"head_source_ranges": "Head 变更源码区间", "base_source_ranges": "Base 删除前源码区间",
                   "file_changes": "文件变更元数据"}
    if digest.checked_areas:
        lines.append("已检查：" + "、".join(area_labels[area] for area in digest.checked_areas) + "。")
    if digest.reviewed_files:
        lines.append("本次目标全部检查完成的文件：" + "、".join(markdown_text(path, 500)
            for path in digest.reviewed_files[:20]) + "。")
        if len(digest.reviewed_files) > 20:
            lines.append(f"另有 {len(digest.reviewed_files) - 20} 个文件，完整列表见发布计划。")
    lines += _goal_lines(digest.examined_goals, "已检查目标")
    if not digest.examined_goals:
        lines.append("未记录已完成的审查目标；检查范围未知或尚未检查。")
    if digest.excluded_files:
        lines += ["", "未纳入本次审查的文件：", *[f"- {markdown_text(item.path, 500)}：{markdown_text(item.reason)}"
                  for item in digest.excluded_files[:20]]]
        if len(digest.excluded_files) > 20:
            lines.append(f"另有 {len(digest.excluded_files) - 20} 个排除项，完整列表见发布计划。")
    rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings = sorted([*plan.inline_drafts, *plan.summary_findings], key=lambda item: (
        rank[item.severity], item.anchor.file_path, item.anchor.start_line or 0, item.finding_key))
    if findings:
        lines += ["", "### 问题索引", ""]
        for index, finding in enumerate(findings, 1):
            anchor = finding.anchor
            location = markdown_text(anchor.file_path, 500)
            if anchor.start_line:
                location += f":{anchor.start_line}（{anchor.side}）"
            lines.append(f"{index}. [{finding.severity.title()}] {finding.title} — {location}"
                         + ("（Summary）" if finding.placement == "summary" else ""))
        for finding in plan.summary_findings:
            lines += ["", render_finding(finding)]
    withheld_count = outcome.confirmed_count - outcome.publishable_count
    if withheld_count > 0:
        lines += ["", f"{withheld_count} 项已确认记录未进入发布草稿；具体扣留原因保留在内部发布计划中。"]
    if outcome.reason_codes:
        lines += ["", "### 仍需关注", "", *[f"- {REASON_LABELS[reason]}（{reason.value}）" for reason in outcome.reason_codes]]
    lines += _goal_lines(digest.incomplete_goals, "未完成目标")
    if outcome.incomplete_investigations:
        lines += ["", f"仍有 {outcome.incomplete_investigations} 项调查未完成，需补齐证据与核验。"]
    if digest.termination_reason and digest.termination_reason != "plan_exhausted":
        lines += ["", f"运行终止记录：{markdown_text(digest.termination_reason, 160)}。"]
    completeness = {"complete": "完整", "partial": "不完整", "unknown": "未知"}
    reliability = {"reliable": "可靠", "unreliable": "存在限制", "unknown": "未知"}
    lines += ["", "### 验证边界", "",
        f"审查完整性：{completeness[outcome.review_completeness]}；运行可靠性：{reliability[outcome.runtime_reliability]}。",
        "仅完成只读静态审查；未执行目标仓库代码、测试或构建。未发现问题不等于证明代码正确。"
        if digest.validation_state == "not_executed" else "动态验证状态未知；不能据此宣称目标仓库测试或构建已通过。"]
    if outcome.kind != "clean":
        lines += ["", "此结果不应视为 Approve 或质量检查通过。"]
    lines += ["", f"审查 Commit：{markdown_text(plan.snapshot.head_sha or '未记录', 64)}。", "",
              f"审查记录：{markdown_text(plan.review_id, 160)}。"]
    return "\n".join(lines)


def _goal_lines(goals, title):
    if not goals:
        return []
    lines = ["", f"{title}："]
    for goal in goals[:20]:
        location = markdown_text(goal.path or "文件未记录", 500)
        if goal.start_line:
            location += f":{goal.start_line}" + (f"–{goal.end_line}" if goal.end_line else "")
        if goal.side:
            location += f"（{goal.side}）"
        reason = f"；{markdown_text(goal.reason)}" if goal.reason else ""
        lines.append(f"- {location}：{markdown_text(goal.status, 100)}{reason}")
    if len(goals) > 20:
        lines.append(f"另有 {len(goals) - 20} 项，完整目标见发布计划。")
    return lines


def render_user_report(task, plan: PublicationPlan) -> str:
    lines = ["# RepoGuardian 用户审查报告", "", plan.summary_markdown]
    for finding in plan.inline_drafts:
        lines += ["", f"### {finding.title}", "", render_finding(finding)]
    lines += ["", "完整证据、被扣留记录与运行过程请查看原始审计报告。"]
    return "\n".join(lines)
