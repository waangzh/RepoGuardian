"""同一事实源的复制、Summary 和 Inline 投影；此模块不执行模型或网络请求。"""

import re

from app.models.publication import PresentationDelivery, ReviewPresentation, SummaryInputCoverage
from app.services.publication_renderer import (
    _comment_history_risk_section, _comment_next_steps, _comment_other_results,
    _comment_risks, _finding_location_text, _ordered_findings, _sentence, _validation_sentence, _goal_lines, _human_lines,
    comment_selection, finding_location, finding_markdown_title, markdown_text,
    merge_verdict_line, partial_review_note, _history_entries,
)


_MISSING_OBSERVATIONS = {
    "未保存可归纳的实际变更。",
    "未保存变更明细，无法归纳实际修改。",
    "尚未形成可引用的实现行为观察。",
    "尚未形成可引用的实现行为观察；以上仅为变更元数据。",
    "尚未形成可引用的测试断言核对记录。",
    "尚未形成测试断言核对记录。",
    "未保存足以概括主要变更的代码观察，请结合固定快照 Diff 人工核对。",
}


def _observation_text(value):
    text = " ".join((value or "").split())
    return "" if text in _MISSING_OBSERVATIONS else text


def _overview_sentences(text):
    return [part for part in re.split(r"(?<=[。！？])\s*|(?<=[.!?])\s+", text) if part.strip()]


def _editorial_markdown(presentation, text, limit=2000):
    rendered = markdown_text(text, limit)
    # Preserve literal URLs without letting GFM create model-supplied links.
    return rendered.replace(":", "&#58;") if presentation.schema_version == "review-presentation-v2" else rendered


def _change_overview(presentation):
    """只从投影选取已有观察；缺少观察时不把作者目标改写成实现事实。"""
    summary = _observation_text(presentation.change_summary)
    if len(summary) > 600:
        summary = _sentence(summary, 600)
    seen = {part.strip().rstrip("。；; .") for part in _overview_sentences(summary)}
    tests = []
    # Frozen v1 plans retain their original body for publication recovery.
    for value in presentation.test_changes if presentation.schema_version == "review-presentation-v1" else []:
        text = _observation_text(value)
        key = text.rstrip("。；; .")
        if key and key not in seen:
            tests.append(text)
            seen.add(key)
    parts = []
    if summary:
        parts.append(summary)
    elif presentation.purpose_text:
        label = {"author_claim": "作者目标为", "code_inferred": "基于代码观察的目的归纳"}.get(
            presentation.purpose_source, "记录中的目标为")
        parts.append(f"{label}：{_sentence(presentation.purpose_text, 300)}")
        parts.append("尚缺少可概括实现变化的代码观察。")
    else:
        parts.append("尚缺少可概括实现变化的代码观察。")
    if tests:
        # 优先保留概述中的完整事实；只在剩余句数内补充测试观察，不截断其条件或否定。
        remaining = max(0, 4 - len(_overview_sentences(" ".join(parts))))
        selected = []
        for text in tests:
            count = len(_overview_sentences(text))
            if count <= remaining and len(" ".join([*selected, text])) <= 400:
                selected.append(text)
                remaining -= count
        if selected:
            text = " ".join(selected)
            parts.append(f"测试源码变化（不表示测试已运行）：{text}")
    return _editorial_markdown(presentation, " ".join(parts), 1200)


def _investigation_view(item):
    proposition = item.proposition
    call_match = re.match(
        r"待源码核对的 calls 命题：调用方\s+([^\s（]+)（定义范围 \d+-\d+）"
        r"对\s+([^\s（]+)（定义范围 \d+-\d+）", proposition)
    is_call_chain = bool(call_match or re.search(r"\bcalls\b|调用链|嵌套调用|调用方", proposition, re.I))
    title = (f"{call_match.group(1).rsplit('::', 1)[-1]} → "
             f"{call_match.group(2).rsplit('::', 1)[-1]} 调用兼容性核验"
             if call_match else proposition if len(proposition) <= 140 else "关联行为核验")
    doubt = ("尚不能确认调用双方的参数传递、返回值消费与异常处理路径是否兼容。"
             if is_call_chain else "尚不能确认该调查命题已获得充分证据，不能据此判断代码存在缺陷。")
    missing = next((_observation_text(value) for value in [*item.limitations, item.reason or ""]
                    if len(value) <= 280 and re.search(r"缺少|未读取|未覆盖|不足|缺失|missing", value, re.I)), "")
    if not missing:
        missing = ("尚缺少足以核验相关调用链的绑定源码观察。" if is_call_chain
                   else "尚缺少足以支持或反驳该调查结论的绑定源码证据。")
    action = (
        "补查调用双方实现，核对参数传递、返回值消费与异常处理，并确认相关回归测试覆盖预期场景。"
        if is_call_chain else
        "补查调查命题涉及的实现与可引用证据；证据充分前继续保持未决，不将其作为已确认缺陷。"
    )
    return {
        "title": markdown_text(title, 180),
        "doubt": markdown_text(doubt, 300),
        "missing": markdown_text(missing, 300),
        "action": markdown_text(action, 360),
    }


def _review_conclusion(plan):
    outcome = plan.outcome
    if (outcome.kind in {"failed", "cancelled"} or outcome.confirmed_count
            or outcome.historical_discussion.affects_risk
            or plan.digest.validation_state in {"failed", "unknown"}):
        return merge_verdict_line(plan)
    if outcome.kind == "clean" and outcome.finding_conclusion == "no_confirmed_findings":
        return "✅ 结论：已检查范围内未发现已确认问题"
    if outcome.finding_conclusion == "no_confirmed_findings":
        if outcome.incomplete_investigations:
            return (f"❓ 结论：未发现已确认代码问题，但有 {outcome.incomplete_investigations} 项"
                    "关联调查尚未完成，暂不能给出完整结论")
        if outcome.unresolved_count:
            return (f"❓ 结论：未发现已确认代码问题，但有 {outcome.unresolved_count} 项候选"
                    "尚待核实，暂不能给出完整结论")
        if outcome.total_goals and outcome.examined_goals < outcome.total_goals:
            remaining = outcome.total_goals - outcome.examined_goals
            return (f"❓ 结论：未发现已确认代码问题，但有 {remaining} 项约定目标尚未检查，"
                    "暂不能给出完整结论")
    return merge_verdict_line(plan)


def compact_finding(finding):
    label = finding.severity.title()
    if finding.category:
        label += " · " + markdown_text(finding.category.title(), 100)
    return (f"**[{label}] {finding_markdown_title(finding)}**\n\n"
            f"**问题：** {markdown_text(finding.affected_behavior)}\n\n"
            f"**影响／触发：** {markdown_text(finding.failure_scenario)}\n\n"
            f"**建议：** {markdown_text(finding.recommendation)}")


def build_presentation(plan, synthesis=None):
    # 原始观察与作者声明保留在 Digest；未经语义检查不自动进入对外摘要。
    purpose = None
    summary = "未形成足以概括主要行为变化的可靠摘要，请结合当前快照 Diff 核对。"
    draft = synthesis.synthesis if synthesis and synthesis.status == "ready" else None
    if draft:
        purpose = draft.pr_purpose.text or None
        summary = draft.change_summary.text
    findings = _ordered_findings(plan)
    limitations = list(dict.fromkeys(_comment_risks(plan, include_investigations=False)))
    input_coverage = None
    if synthesis and (synthesis.input_truncated or synthesis.truncation_reasons):
        input_coverage = SummaryInputCoverage(
            truncation_reasons=synthesis.truncation_reasons,
            omitted_files=synthesis.omitted_files,
            omitted_hunks=synthesis.omitted_hunks)
    return ReviewPresentation(
        schema_version="review-presentation-v2", review_id=plan.review_id, snapshot=plan.snapshot,
        synthesis_status=synthesis.status if synthesis else "fallback",
        purpose_text=purpose,
        purpose_source=draft.pr_purpose.source_kind if draft else "unknown",
        change_summary=summary or "未保存足以概括主要变更的代码观察，请结合固定快照 Diff 人工核对。",
        review_summary=draft.review_summary.text if draft else "",
        verdict=plan.digest.merge_decision, findings=findings,
        delivery=PresentationDelivery(confirmed=plan.outcome.confirmed_count, eligible=len(findings),
            selected=len(findings), inline_ready=len(plan.inline_drafts),
            summary_only=len(plan.summary_findings), not_selected=0),
        validation_state=plan.digest.validation_state, limitations=limitations,
        summary_input_coverage=input_coverage)


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
    lines += ["### 本次 PR 主要变更", "", _change_overview(presentation), ""]
    delivery = presentation.delivery
    count = len(findings)
    views = {item.id: item for item in plan.digest.findings}
    verdict = _review_conclusion(plan)
    if count < delivery.eligible:
        verdict += (f"；本轮已确认 {delivery.confirmed} 项，本次{'包含' if copy else '选择'} {count} 项"
                    f"；未选择的问题仍属于审查结论，不能视为已修复或已投递")
    lines += ["### 审查结果", "", verdict + "。", ""]
    if detail == "full":
        lines += [f"本轮已确认 {delivery.confirmed} 项；具备发布资格 {delivery.eligible} 项；"
                  f"本次包含 {count} 项；未包含 {delivery.eligible - count} 项。", ""]
        if presentation.review_summary and findings:
            lines += [f"问题概述：{_editorial_markdown(presentation, presentation.review_summary, 600)}", ""]
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
    history_risks = _history_entries(plan)[0]
    lines += _comment_history_risk_section(plan, history_risks)
    scope_lines = []
    if detail == "full":
        scope_lines = ["", "### 审查范围", ""]
        scope_lines += _goal_lines(plan.digest.examined_goals, "已检查目标", limit=None)
        scope_lines += _goal_lines(plan.digest.incomplete_goals, "未完成目标", limit=None)
        scope_lines += [f"- 排除文件：{markdown_text(item.path)}；{markdown_text(item.reason)}"
                        for item in plan.digest.excluded_files]
    lines += _comment_other_results(plan, omitted=omitted, retained=retained,
        withheld=max(0, plan.outcome.confirmed_count - plan.outcome.publishable_count),
        full=detail == "full", include_discussed=include_discussed, extra_lines=scope_lines,
        include_investigations=not any(item.answer == "unresolved" for item in plan.digest.investigations))
    pending = [item for item in plan.digest.investigations if item.answer == "unresolved"]
    if pending:
        lines += ["", f"## 待核实事项（{len(pending)} 项）", ""]
        for item in pending[:5]:
            view = _investigation_view(item)
            lines += [f"### {view['title']}", "",
                f"**疑点：** {view['doubt']}", "",
                f"**缺少的证据：** {view['missing']}", "",
                f"**建议：** {view['action']}", ""]
        if len(pending) > 5:
            lines += [f"- 另有 {len(pending) - 5} 项待核实事项；完整命题、证据与请求范围见内部技术审计记录。", ""]
    coverage = presentation.summary_input_coverage
    if presentation.synthesis_status == "ready" and coverage and coverage.limits_diff_summary:
        lines += ["变更概述仅覆盖已纳入的 Diff 片段；这不表示静态审查范围不完整。", ""]
    if detail == "full":
        advice = presentation.verdict.advice
        scope = (f"约定目标已检查 {plan.outcome.examined_goals} / {plan.outcome.total_goals}。"
                 if plan.outcome.total_goals else "缺少有效目标记录，检查范围未知。")
        lines += ["", "### 验证与合并建议", "",
            f"- 静态审查：{scope}",
            "- 动态验证：" + _validation_sentence(plan),
            "- 合并建议：" + markdown_text(advice), "", "---", "",
            f"审查快照：`{markdown_text(plan.snapshot.head_sha or '未记录', 64)}`。"]
    else:
        scope = (f"静态目标已检查 {plan.outcome.examined_goals}/{plan.outcome.total_goals}"
                 if plan.outcome.total_goals else "静态检查范围未知")
        dynamic = _validation_sentence(plan)
        if pending and presentation.verdict.status == "resolve_unknowns" \
                and plan.outcome.kind not in {"failed", "cancelled"} \
                and plan.digest.validation_state not in {"failed", "unknown"}:
            advice = "建议先补齐上述待核实事项所需证据，再结合 CI 和人工评审决定是否合并。"
        else:
            advice = " ".join(_comment_next_steps(
                plan, has_findings=bool(_ordered_findings(plan) or history_risks)))
        lines += ["", "### 验证与合并建议", "",
            f"{scope}；动态验证：{dynamic} {markdown_text(advice)}", "", "---", "",
            f"审查快照：`{markdown_text(plan.snapshot.head_sha or '未记录', 64)}`"
            + (" · 本次未纳入历史讨论" if plan.digest.discussion_review_enabled is False else "")
            + " · 结论仅适用于当前快照。"]
    if plan.digest.discussion_review_enabled is False:
        if detail == "full":
            lines += ["本次未纳入历史讨论；结论不包含历史讨论复核。"]
    if copy and detail == "full":
        lines += ["复制完整 Markdown 评论只生成文本，不会自动创建 GitHub 代码行讨论。"]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
