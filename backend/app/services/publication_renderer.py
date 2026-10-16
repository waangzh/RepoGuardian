"""仅渲染后端已准入文本；不访问 GitHub、模型或仓库。"""

import html
import re
import unicodedata

from app.models.publication import PublicationFinding, PublicationPlan


OUTCOME_LABELS = {"clean": "未发现可行动问题", "findings": "发现已确认问题", "inconclusive": "审查结论不确定",
                  "failed": "审查失败", "cancelled": "审查已取消"}
REASON_LABELS = {
    "task_failed": "任务或运行时执行失败", "task_cancelled": "任务或运行时已取消",
    "termination_incomplete": "任务未可靠完成全部计划", "scope_incomplete": "部分目标未完成检查",
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
    lines = ["## RepoGuardian Review", "", f"**{OUTCOME_LABELS[outcome.kind]}**", "",
             f"- 目标检查：{outcome.examined_goals} / {outcome.total_goals}",
             f"- 已确认问题：{outcome.confirmed_count}", f"- 可生成草稿：{outcome.publishable_count}",
             f"- 未决候选：{outcome.unresolved_count}", f"- 未完成调查：{outcome.incomplete_investigations}",
             f"- 覆盖状态：{outcome.coverage.value}"]
    findings = [*plan.inline_drafts, *plan.summary_findings]
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
        lines += ["", "### 限制条件", "", *[f"- {REASON_LABELS[reason]}" for reason in outcome.reason_codes]]
    if outcome.kind == "clean":
        lines += ["", "在已审查变更中未发现可行动问题。"]
    elif outcome.kind != "findings":
        lines += ["", "未产生可发布问题不代表审查通过。"]
    lines += ["", f"审查 Commit：{markdown_text(plan.snapshot.head_sha or '未记录', 64)}。", "",
              "> 仅进行了静态审查，未执行目标仓库代码。此结果不保证不存在缺陷。"]
    return "\n".join(lines)


def render_user_report(task, plan: PublicationPlan) -> str:
    lines = ["# RepoGuardian 用户审查报告", "", plan.summary_markdown]
    for finding in plan.inline_drafts:
        lines += ["", f"### {finding.title}", "", render_finding(finding)]
    lines += ["", "完整证据、被扣留记录与运行过程请查看原始审计报告。"]
    return "\n".join(lines)
