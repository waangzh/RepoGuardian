"""Thin formatting for Publisher-approved findings only."""

import html
import re


def markdown_text(value, limit=4000):
    text = html.escape(value[:limit], quote=False).replace("@", "＠")
    return re.sub(r"([\\`*_\[\]()!#|])", r"\\\1", text)


def _findings(plan):
    return [*plan.inline_drafts, *plan.summary_findings]


def render_finding(finding):
    return finding.body_markdown


def render_summary(plan, *, discussion_urls=None):
    findings = _findings(plan)
    lines = ["## RepoGuardian Summary", "",
        f"审查报告版本：`{plan.report_version}`",
        f"固定 Head：`{plan.snapshot.head_sha or 'unknown'}`", "",
        "### 变更总览", ""]
    if plan.overview_markdown:
        lines += [plan.overview_markdown, ""]
    else:
        lines += [f"LLM 总览未能生成（{markdown_text(plan.overview_status)}），审查事实已保留。",
                  "", *[f"- {markdown_text(item, 500)}" for item in plan.digest.actual_changes[:100]], ""]
    if plan.outcome.coverage != "complete" or plan.scope_reasons:
        lines += ["**结论不完整或存在范围说明。**", "",
                  *[f"- {markdown_text(reason, 500)}" for reason in plan.scope_reasons[:100]], ""]
    if plan.outcome.unresolved_count:
        lines += [f"**存在 {plan.outcome.unresolved_count} 个未决问题。**", ""]
    if findings:
        lines += ["### 已验证问题", ""]
        for finding in findings:
            location = (f"（{finding.anchor.file_path}:{finding.anchor.start_line}）"
                        if finding.anchor.start_line else "")
            lines += [f"- **{finding.severity.title()}** {markdown_text(finding.title, 300)} "
                      f"{markdown_text(location, 600)}"]
    else:
        lines.append("本轮没有可发布的已验证问题。")
    if plan.withheld_findings:
        lines += ["", "### 未发布记录", ""]
        lines += [f"- `{item.candidate_id}`：{item.reason_code}" for item in plan.withheld_findings]
    lines += ["", "未执行目标仓库代码、测试或构建；投递状态独立于审查结论。"]
    return "\n".join(lines)


def render_review_comment(plan, *, detail="compact", exclude_resolved=False):
    return render_summary(plan)


def render_finding_comment(plan, finding):
    return finding.body_markdown


def comment_selection(plan, *, exclude_resolved=False):
    return _findings(plan), [], []


def render_user_report(run, plan):
    lines = ["# RepoGuardian 审查报告", "", render_summary(plan), "", "### 固定变更", ""]
    lines += [f"- {markdown_text(item, 500)}" for item in plan.digest.actual_changes]
    return "\n".join(lines)


def discussion_report_lines(plan, task=None):
    return []
