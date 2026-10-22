"""同一事实源的复制、Summary 和 Inline 投影；此模块不执行模型或网络请求。"""

import re

from app.models.publication import PresentationDelivery, ReviewPresentation
from app.services.publication_renderer import (
    _comment_change_overview, _comment_history_risk_section, _comment_other_results,
    _comment_risks, _finding_location_text, _ordered_findings, _validation_sentence, _goal_lines, _human_lines,
    comment_selection, finding_location, finding_markdown_title, markdown_text,
    merge_verdict_line, partial_review_note, _history_entries,
)


def compact_finding(finding):
    label = finding.severity.title()
    if finding.category:
        label += " · " + markdown_text(finding.category.title(), 100)
    return (f"**[{label}] {finding_markdown_title(finding)}**\n\n"
            f"**问题：** {markdown_text(finding.affected_behavior)}\n\n"
            f"**影响／触发：** {markdown_text(finding.failure_scenario)}\n\n"
            f"**建议：** {markdown_text(finding.recommendation)}")


def build_presentation(plan, synthesis=None):
    understanding = plan.digest.change_understanding
    purpose = understanding.author_goal if understanding else None
    if purpose and (purpose == "未记录明确的作者目标。" or re.search(
            r"found by.*audit|deep.review audit|审计发现", purpose, re.I)):
        purpose = None
    summary = (understanding.implementation_changes or understanding.actual_changes) if understanding else ""
    test_changes = [understanding.test_observations] if understanding and understanding.test_observations else []
    draft = synthesis.synthesis if synthesis and synthesis.status == "ready" else None
    if draft:
        purpose = draft.pr_purpose.text or None
        summary = draft.change_summary.text
        test_changes = [item.text for item in draft.test_changes]
    findings = _ordered_findings(plan)
    limitations = list(dict.fromkeys(_comment_risks(plan)))
    if synthesis and synthesis.input_truncated:
        limitations.append("- 摘要输入经过截断；变更概述仅覆盖已提供的资料。")
    return ReviewPresentation(
        review_id=plan.review_id, snapshot=plan.snapshot,
        synthesis_status=synthesis.status if synthesis else "fallback",
        purpose_text=purpose,
        purpose_source=draft.pr_purpose.source_kind if draft else "author_claim" if purpose else "unknown",
        change_summary=summary or "未保存足以概括主要变更的代码观察，请结合固定快照 Diff 人工核对。",
        test_changes=test_changes, review_summary=draft.review_summary.text if draft else "",
        verdict=plan.digest.merge_decision, findings=findings,
        delivery=PresentationDelivery(confirmed=plan.outcome.confirmed_count, eligible=len(findings),
            selected=len(findings), inline_ready=len(plan.inline_drafts),
            summary_only=len(plan.summary_findings), not_selected=0),
        validation_state=plan.digest.validation_state, limitations=limitations)


def selected_presentation(plan, selected, fallback=(), *, already_published=0, actual_published=None):
    presentation = plan.presentation.model_copy(deep=True)
    keys, fallback = set(selected), set(fallback)
    presentation.findings = [finding.model_copy(update={"placement": "summary"})
        if finding.finding_key in fallback else finding
        for finding in presentation.findings if finding.finding_key in keys]
    count = sum(item.placement == "inline" for item in presentation.findings)
    presentation.delivery = PresentationDelivery(
        confirmed=plan.outcome.confirmed_count, eligible=len(_ordered_findings(plan)),
        selected=len(presentation.findings), inline_ready=max(0, count - already_published),
        summary_only=len(presentation.findings) - count,
        not_selected=len(_ordered_findings(plan)) - len(presentation.findings),
        already_published=already_published, actual_published=actual_published)
    return presentation


def render_presentation(plan, *, copy=False, detail="compact", exclude_resolved=False,
                        include_discussed=False, discussion_urls=None):
    presentation = plan.presentation
    included, omitted, retained = comment_selection(plan, exclude_resolved=exclude_resolved)
    selected = {item.finding_key for item in included}
    findings = [item for item in presentation.findings if item.finding_key in selected]
    lines = ["## RepoGuardian · PR Review", ""]
    note = partial_review_note(plan)
    if note:
        lines += [note, ""]
    lines += ["### 本次 PR 主要变更", ""]
    if presentation.purpose_text:
        label = "作者称" if presentation.purpose_source == "author_claim" else "基于代码观察的目的归纳"
        lines += [f"{label}：{markdown_text(presentation.purpose_text, 500)}", ""]
    if presentation.synthesis_status == "ready":
        lines += [markdown_text(presentation.change_summary, 1200), ""]
        if presentation.test_changes:
            lines += [f"测试源码变化：{markdown_text(' '.join(presentation.test_changes), 800)}"
                      "（不表示测试已运行）", ""]
    else:
        # 确定性回退复用已保存的来源标签与观察，不把背景句硬充为 PR 目的。
        overview = _comment_change_overview(plan, title="本次 PR 主要变更")[2:]
        if presentation.purpose_text is None:
            overview = [line for line in overview if not line.startswith("**PR 目标：**")]
        lines += overview + ["", "摘要来源：已保存事实的确定性回退。", ""]
    lines += ["### 审查结果", "", merge_verdict_line(plan), ""]
    delivery = presentation.delivery
    count = len(findings)
    views = {item.id: item for item in plan.digest.findings}
    lines += [f"本轮已确认 {delivery.confirmed} 项；具备发布资格 {delivery.eligible} 项；"
              f"本次{'包含' if copy else '选择'} {count} 项；"
              f"未{'包含' if copy else '选择'} {delivery.eligible - count} 项。", ""]
    if presentation.review_summary and findings:
        lines += [f"问题概述：{markdown_text(presentation.review_summary, 600)}", ""]
    if delivery.not_selected:
        lines += ["未选择的问题仍属于审查结论，不能视为已修复或已投递。", ""]
    if not findings:
        lines += [("已检查范围内未发现已确认问题。"
                   if plan.outcome.kind == "clean" else "本次没有选择可交付的问题条目；请结合下方结论边界判断。"), ""]
    for finding in findings:
        if copy or finding.placement == "summary":
            lines += [finding.body_markdown, "", finding_location(plan, finding), ""]
            if not copy:
                lines += ["仅在 Summary 中反馈。", ""]
            if detail == "full" and finding.evidence_summary:
                lines += ["代码证据：" + markdown_text(finding.evidence_summary), ""]
            if copy and (view := views.get(finding.finding_key)):
                lines += _human_lines(view)
        else:
            url = (discussion_urls or {}).get(finding.finding_key)
            lines += [f"- **[{finding.severity.title()}] {finding_markdown_title(finding)}** — "
                      + _finding_location_text(finding)
                      + (f" · [查看代码讨论]({url})" if url else "（代码行意见以实际发布回执为准）")]
        if finding.default_selection_reason == "historical_duplicate" or include_discussed:
            from app.services.pr_discussion_matching import safe_discussion_url
            urls = [safe for url in finding.discussion_urls
                    if (safe := safe_discussion_url(url, plan.digest.pr_url))]
            links = "、".join(f"[历史讨论 {index}]({url})" for index, url in enumerate(urls, 1))
            if links:
                lines += ["", "当前快照仍确认此问题；已有讨论不代表已修复。" + links, ""]
    lines += _comment_history_risk_section(plan, _history_entries(plan)[0])
    scope_lines = []
    if detail == "full":
        scope_lines = ["", "### 审查范围", ""]
        scope_lines += _goal_lines(plan.digest.examined_goals, "已检查目标", limit=None)
        scope_lines += _goal_lines(plan.digest.incomplete_goals, "未完成目标", limit=None)
        scope_lines += [f"- 排除文件：{markdown_text(item.path)}；{markdown_text(item.reason)}"
                        for item in plan.digest.excluded_files]
    lines += _comment_other_results(plan, omitted=omitted, retained=retained,
        withheld=max(0, plan.outcome.confirmed_count - plan.outcome.publishable_count),
        full=detail == "full", include_discussed=include_discussed, extra_lines=scope_lines)
    # 影响主结论的未决命题在简洁 Summary 中仍需可见。
    pending = [item for item in plan.digest.investigations if item.answer == "unresolved"]
    if pending:
        lines += ["", "### 未决调查", ""]
        for item in pending[:5]:
            lines += ["- " + markdown_text(item.proposition)
                      + "；" + markdown_text(item.reason or "缺少充分证据")]
        if len(pending) > 5:
            lines += [f"- 另有 {len(pending) - 5} 项，完整命题与原因见内部报告。"]
    if any("摘要输入经过截断" in line for line in presentation.limitations):
        lines += ["", "摘要输入经过截断，变更概述不覆盖完整 PR。"]
    lines += ["", "### 验证与合并建议", "",
        f"- 静态审查：约定目标已检查 {plan.outcome.examined_goals} / {plan.outcome.total_goals}。",
        "- 动态验证：" + _validation_sentence(plan),
        "- 未获取的 GitHub CI 状态保持未知；本结果不替代 CI 与人工审批。",
        "- 合并建议：" + markdown_text(presentation.verdict.advice), "", "---", "",
        f"审查快照：`{markdown_text(plan.snapshot.head_sha or '未记录', 64)}`。"]
    if plan.digest.discussion_review_enabled is False:
        lines += ["本次未纳入历史讨论；结论不包含历史讨论复核。"]
    if copy:
        lines += ["复制完整 Markdown 评论只生成文本，不会自动创建 GitHub 代码行讨论。"]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
