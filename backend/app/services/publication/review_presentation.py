"""同一事实源的复制、Summary 和 Inline 投影；此模块不执行模型或网络请求。"""

import re

from app.models.publication import PresentationDelivery, ReviewPresentation
from app.review.observations import fallback_observations, observation_gaps, observation_text
from app.review.synthesis import synthesis_materials, validate_grounding, validate_synthesis
from app.services.publication.review_prose import author_goal_text
from app.services.publication.review_text_format import (
    _comment_risks, _finding_location_text, _ordered_findings, _sentence, STATIC_BOUNDARY,
    markdown_text,
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
    return rendered.replace(":", "&#58;")


def _trim_summary(text, limit=600):
    """按整句保留到上限，避免只剩首句而丢掉条件或否定。"""
    if len(text) <= limit:
        return text
    kept = ""
    for part in _overview_sentences(text):
        if len(kept) + len(part) > limit:
            break
        kept += part
    return kept or _sentence(text, limit)


def _change_overview(presentation):
    """只从投影选取已有观察；缺少观察时不把作者目标改写成实现事实。"""
    summary = _observation_text(presentation.change_summary)
    if presentation.summary_source == "metadata":
        summary = _trim_summary(summary)
    seen = {part.strip().rstrip("。；; .") for part in _overview_sentences(summary)}
    tests = []
    for value in presentation.test_changes:
        text = _observation_text(value)
        key = text.rstrip("。；; .")
        if key and key not in seen:
            tests.append(text)
            seen.add(key)
    parts = []
    if summary:
        parts.append(("变更范围：" if presentation.summary_source == "metadata" else "") + summary)
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
    return _editorial_markdown(presentation, " ".join(parts), 12000)


def _review_conclusion(plan):
    from app.services.publication.publication_renderer import merge_verdict_line

    outcome = plan.outcome
    if outcome.kind in {"failed", "cancelled"} or outcome.confirmed_count:
        return merge_verdict_line(plan)
    if outcome.kind == "clean" and outcome.finding_conclusion == "no_confirmed_findings":
        return "✅ 结论：已检查范围内未发现已确认问题"
    if outcome.finding_conclusion == "no_confirmed_findings":
        if outcome.incomplete_investigations:
            return (f"❓ 结论：未发现已确认代码问题，但有 {outcome.incomplete_investigations} 项"
                    "必要证据缺口尚未解决，暂不能给出完整结论")
        if outcome.pending_limitation_count:
            return (f"❓ 结论：未发现已确认代码问题，但有 {outcome.pending_limitation_count} 条"
                    "审查限制声明尚未完成必要性复核，暂不能给出完整结论")
        if outcome.unresolved_count:
            return (f"❓ 结论：未发现已确认代码问题，但有 {outcome.unresolved_count} 项候选"
                    "尚待核实，暂不能给出完整结论")
        if outcome.total_goals and outcome.examined_goals < outcome.total_goals:
            remaining = outcome.total_goals - outcome.examined_goals
            return (f"❓ 结论：未发现已确认代码问题，但有 {remaining} 项约定目标尚未检查，"
                    "暂不能给出完整结论")
    return merge_verdict_line(plan)


def _review_summary(plan):
    """问题概述由已核验问题确定性生成，模型不再改写问题结论。"""
    findings = _ordered_findings(plan)
    if not findings:
        return ""
    titles = "；".join(item.title[:120] for item in findings[:3])
    more = f" 等 {len(findings)} 项" if len(findings) > 3 else ""
    return f"本轮已确认 {len(findings)} 项问题：{titles}{more}。"


def _fallback_summary(plan):
    """回退只用冻结 Diff 派生的确定性变更范围。

    审查阶段的模型散文仍保留在 Digest 与详细报告中供审计，
    但不作为对外 PR 摘要，避免未经约束的结论进入交付。
    """
    understanding = plan.digest.change_understanding
    diff_summary = _observation_text(understanding.diff_summary) if understanding else ""
    if diff_summary:
        return diff_summary, "metadata"
    return "未保存足以概括主要变更的代码观察，请结合固定快照 Diff 人工核对。", "unavailable"


def build_presentation(plan, *, report=None, observations=()):
    understanding = plan.digest.change_understanding
    purpose = (author_goal_text(understanding.author_goal) if understanding else "") or None
    summary, summary_source = _fallback_summary(plan)
    fallback = fallback_observations(report, observations)
    material_incomplete = bool(report and observation_gaps(report, observations))
    if fallback and not material_incomplete:
        summary = "\n".join(observation_text(item) for item in fallback[:6])
        summary_source = "observations"
    review_summary = _review_summary(plan)
    action_summary = ""
    synthesis = report.synthesis.model_copy(deep=True) if report and report.synthesis else None
    synthesis_status = "fallback"
    if synthesis and synthesis.status == "ready":
        version, material = synthesis_materials(report, plan, observations)
        try:
            if material_incomplete:
                raise ValueError("synthesis_observations_incomplete")
            if synthesis.material_version != version:
                raise ValueError("synthesis_material_changed")
            output = validate_synthesis(synthesis.output, material)
            validate_grounding(synthesis, output)
            summary = "\n\n".join(item.text for item in output.change_overview)
            review_summary = "\n\n".join(item.text for item in output.review_overview)
            action_summary = "\n\n".join(item.text for item in output.actions)
            summary_source, synthesis_status = "model", "ready"
        except ValueError:
            synthesis.status, synthesis.error = "fallback", "synthesis_material_changed"
    findings = _ordered_findings(plan)
    limitations = list(dict.fromkeys(_comment_risks(plan)))
    return ReviewPresentation(
        review_id=plan.review_id, snapshot=plan.snapshot,
        synthesis_status=synthesis_status, synthesis=synthesis, observations=list(observations),
        summary_source=summary_source,
        purpose_text=purpose,
        purpose_source="author_claim" if purpose else "unknown",
        change_summary=summary or "未保存足以概括主要变更的代码观察，请结合固定快照 Diff 人工核对。",
        # 问题概述只由确定性 finding 生成；编辑摘要不再改写问题结论。
        review_summary=review_summary, action_summary=action_summary,
        test_changes=([observation_text(item) for item in fallback if item.kind == "test_source"]
                      if not material_incomplete and synthesis_status != "ready" else []),
        verdict=plan.digest.merge_decision, findings=findings,
        delivery=PresentationDelivery(confirmed=plan.outcome.confirmed_count, eligible=len(findings),
            selected=len(findings), inline_ready=len(plan.inline_drafts),
            summary_only=len(plan.summary_findings), not_selected=0),
        limitations=limitations)


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
                        discussion_urls=None):
    from app.services.publication.publication_renderer import (
        _comment_next_steps, _comment_other_results, _goal_lines,
        _human_lines, comment_selection, finding_location,
        finding_markdown_title, partial_review_note,
    )

    presentation = plan.presentation
    included, omitted, retained = comment_selection(plan, exclude_resolved=exclude_resolved)
    selected = {item.finding_key for item in included}
    findings = [item for item in presentation.findings if item.finding_key in selected]
    lines = ["## RepoGuardian · PR Review", ""]
    note = partial_review_note(plan)
    if note:
        lines += [note, ""]
    title = "本次 PR 主要变更" if presentation.synthesis_status == "ready" else "本次 PR 变更范围（总览未完整生成）"
    lines += [f"### {title}", "", _change_overview(presentation), ""]
    delivery = presentation.delivery
    count = len(findings)
    views = {item.id: item for item in plan.digest.findings}
    verdict = _review_conclusion(plan)
    if count < delivery.eligible:
        verdict += (f"；本轮已确认 {delivery.confirmed} 项，本次{'包含' if copy else '选择'} {count} 项"
                    f"；未选择的问题仍属于审查结论，不能视为已修复或已投递")
    lines += ["### 审查结果", "", verdict + "。", ""]
    if presentation.review_summary:
        lines += [_editorial_markdown(presentation, presentation.review_summary, 6000), ""]
    if presentation.action_summary:
        lines += ["处理重点：" + _editorial_markdown(presentation, presentation.action_summary, 6000), ""]
    if presentation.synthesis_status != "ready":
        lines += ["变更总览未生成，以上为已保存观察或变更范围；具体问题和检查限制见下方。", ""]
    if presentation.synthesis and any(presentation.synthesis.omitted.values()):
        lines += ["总览材料存在省略，完整问题与证据以下方记录为准。", ""]
    if detail == "full":
        lines += [f"本轮已确认 {delivery.confirmed} 项；具备发布资格 {delivery.eligible} 项；"
                  f"本次包含 {count} 项；未包含 {delivery.eligible - count} 项。", ""]
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
    scope_lines = []
    if detail == "full":
        scope_lines = ["", "### 审查范围", ""]
        scope_lines += _goal_lines(plan.digest.examined_goals, "已检查目标", limit=None)
        scope_lines += _goal_lines(plan.digest.incomplete_goals, "未完成目标", limit=None)
        scope_lines += [f"- 排除文件：{markdown_text(item.path)}；{markdown_text(item.reason)}"
                        for item in plan.digest.excluded_files]
    lines += _comment_other_results(plan, omitted=omitted, retained=retained,
        withheld=max(0, plan.outcome.confirmed_count - plan.outcome.publishable_count),
        full=detail == "full", extra_lines=scope_lines)
    if detail == "full":
        advice = presentation.verdict.advice
        scope = (f"约定目标已检查 {plan.outcome.examined_goals} / {plan.outcome.total_goals}。"
                 if plan.outcome.total_goals else "缺少有效目标记录，检查范围未知。")
        lines += ["", "### 验证与合并建议", "",
            f"- 静态审查：{scope}",
            "- 静态审查边界：" + STATIC_BOUNDARY,
            "- 合并建议：" + markdown_text(advice), "", "---", "",
            f"审查快照：`{markdown_text(plan.snapshot.head_sha or '未记录', 64)}`。"]
    else:
        scope = (f"静态目标已检查 {plan.outcome.examined_goals}/{plan.outcome.total_goals}"
                 if plan.outcome.total_goals else "静态检查范围未知")
        dynamic = STATIC_BOUNDARY
        advice = " ".join(_comment_next_steps(plan, has_findings=bool(_ordered_findings(plan))))
        lines += ["", "### 验证与合并建议", "",
            f"{scope}；静态审查边界：{dynamic} {markdown_text(advice)}", "", "---", "",
            f"审查快照：`{markdown_text(plan.snapshot.head_sha or '未记录', 64)}`"
            + (" · 本次未纳入历史讨论" if plan.digest.discussion_review_enabled is False else "")
            + " · 结论仅适用于当前快照。"]
    if plan.digest.discussion_review_enabled is False:
        if detail == "full":
            lines += ["本次未纳入历史讨论；结论不包含历史讨论复核。"]
    if copy and detail == "full":
        lines += ["复制完整 Markdown 评论只生成文本，不会自动创建 GitHub 代码行讨论。"]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
