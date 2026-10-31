"""Thin formatting for Publisher-approved findings only."""

import html
import re


def markdown_text(value, limit=4000):
    text = html.escape(value[:limit], quote=False).replace("@", "＠")
    return re.sub(r"([\\`*_\[\]()!#|])", r"\\\1", text)


def overview_markdown(value, limit=1000):
    """Preserve only single-backtick code; escape all other Markdown and HTML."""
    def prose(text):
        return re.sub(r"([+\-.:])", r"\\\1", markdown_text(text, limit))

    text, parts, start = value[:limit].replace("\r\n", "\n").replace("\r", "\n"), [], 0
    for match in re.finditer(r"(?<!`)`([^`\r\n]+)`(?!`)", text):
        parts.extend((prose(text[start:match.start()]),
                      "`" + match[1].replace("@", "＠") + "`"))
        start = match.end()
    parts.append(prose(text[start:]))
    return "".join(parts)


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
    if any(plan.overview_omitted.values()):
        lines += ["总览输入有材料省略，仅概括已提供的变更，不代表完整变更范围。", ""]
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
            if finding.placement == "summary":
                lines += ["", finding.body_markdown, ""]
            url = (discussion_urls or {}).get(finding.finding_key)
            if isinstance(url, str) and re.fullmatch(
                r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*#discussion_r[0-9]+", url):
                lines.append(f"  [查看代码讨论]({url})")
    else:
        lines.append("本轮没有可发布的已验证问题。")
    if plan.withheld_findings:
        lines += ["", "### 未发布记录", ""]
        lines += [f"- `{item.candidate_id}`：{item.reason_code}" for item in plan.withheld_findings]
    if plan.historical_findings:
        lines += ["", "### 历史意见", ""]
        for item in plan.historical_findings:
            state = "已在本轮重新核验，结论见本轮结果" if item["rechecked"] else "本轮尚未完成核验"
            lines += [f"- {markdown_text(item['claim'], 300)}：{state}；历史 Head "
                      f"`{markdown_text(item['head_sha'], 64)}`"]
            url = item.get("url")
            if isinstance(url, str) and re.fullmatch(
                r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*#discussion_r[0-9]+", url):
                lines.append(f"  [查看历史讨论]({url})")
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
    if run.report and run.report.overview.omitted_materials:
        lines += ["", "### 总览省略范围", ""]
        reasons = {"material_capacity": "材料容量不足", "source_unavailable": "源码不可用",
                   "diff_source_mismatch": "Diff 与源码不一致"}
        for item in run.report.overview.omitted_materials:
            location = f" {item.side.upper()} {item.start_line}–{item.end_line}" if item.side else ""
            lines.append(f"- {markdown_text(item.path, 500)}{location}：{reasons[item.reason]}")
    return "\n".join(lines)


def discussion_report_lines(plan, task=None):
    return []
