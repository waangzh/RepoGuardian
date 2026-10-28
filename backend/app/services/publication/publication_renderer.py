"""仅渲染后端已准入文本；不访问 GitHub、模型或仓库。"""

import re
from urllib.parse import quote

from app.models.publication import PublicationFinding, PublicationPlan
from app.review.errors import execution_error_text
from app.services.publication.review_prose import author_goal_text
from app.services.publication.review_text_format import (
    MERGE_SYMBOLS, OUTCOME_DESCRIPTIONS, REASON_LABELS,
    _comment_risks, _finding_location_text, _ordered_findings, _sentence, STATIC_BOUNDARY,
    markdown_text, review_limitation_text,
)




def outcome_description(plan) -> str:
    """展示当前审查结论；计量未知只作为运行遥测，不改变代码核验结论。"""
    return OUTCOME_DESCRIPTIONS[plan.outcome.kind]






def render_finding(finding: PublicationFinding) -> str:
    """正文已由 Planner 从 verified publication 文本生成，不再回退候选。"""
    return finding.body_markdown


def finding_markdown_title(finding: PublicationFinding) -> str:
    """纯文本标题只在 Markdown 输出边界转义。"""
    return markdown_text(finding.title, 200)


def finding_body(text: dict, severity: str, *, category: str | None = None,
                 evidence_summary: str | None = None) -> str:
    """单条问题的完整正文；免责声明与 Commit 只在交付文档末尾出现一次。"""
    label = f"{severity.title()} · {markdown_text(category.title(), 100)}" if category else severity.title()
    evidence = f"**代码证据（核验依据）：** {markdown_text(evidence_summary)}\n\n" if evidence_summary else ""
    return (f"**[{label}] {markdown_text(text['claim'], 200)}**\n\n"
            f"**问题与影响：** {markdown_text(text['affected_behavior'])}\n\n"
            + evidence +
            f"**触发场景：** {markdown_text(text['failure_scenario'])}\n\n"
            f"**建议：** {markdown_text(text['recommendation'])}")


def _validation_lines(plan):
    completeness = {"complete": "完整", "partial": "不完整", "unknown": "未知"}
    reliability = {"reliable": "可靠", "unreliable": "存在限制", "unknown": "未知"}
    return [f"审查完整性：{completeness[plan.outcome.review_completeness]}；"
            f"运行可靠性：{reliability[plan.outcome.runtime_reliability]}。",
        "未执行目标仓库代码、测试或构建；未发现问题不等于证明代码正确。"]


def _goal_lines(goals, title, *, limit=20, include_reason=True):
    if not goals:
        return []
    lines = ["", f"{title}："]
    for goal in goals[:limit]:
        location = markdown_text(goal.path or "文件未记录", 500)
        if goal.start_line:
            location += f":{goal.start_line}" + (f"–{goal.end_line}" if goal.end_line else "")
        if goal.side:
            location += f"（{goal.side}）"
        reason = f"；{markdown_text(goal.reason)}" if include_reason and goal.reason else ""
        status = {"examined": "已检查", "not_examined": "未检查", "blocked": "受阻",
                  "unsupported": "暂不支持", "unknown": "未知"}.get(goal.status, goal.status)
        lines.append(f"- {location}：{markdown_text(status, 100)}{reason}")
    if limit is not None and len(goals) > limit:
        lines.append(f"另有 {len(goals) - limit} 项，完整目标见用户报告。")
    return lines






def _location_href(plan, finding) -> str | None:
    anchor = finding.anchor
    # 仅从严格解析的 GitHub PR 身份生成链接，不接受仓库提供的 URL。
    from app.tools.github.github_tool import parse_pr_url
    try:
        owner, repo, _ = parse_pr_url(plan.digest.pr_url or "")
    except ValueError:
        return None
    sha = plan.snapshot.effective_base_sha if anchor.side == "base" else plan.snapshot.head_sha
    if owner and repo and sha and re.fullmatch(r"[a-f0-9]{40,64}", sha) and anchor.start_line and anchor.side:
        return f"https://github.com/{quote(owner, safe='')}/{quote(repo, safe='')}/blob/{sha}/{quote(anchor.file_path, safe='/')}#L{anchor.start_line}"
    return None


def finding_location(plan, finding):
    label = _finding_location_text(finding)
    href = _location_href(plan, finding)
    return f"**代码位置：** [{label}]({href})" if href else f"**代码位置：** {label}"


def merge_heading(plan):
    decision = plan.digest.merge_decision
    return decision.label, decision.advice


def merge_verdict_line(plan) -> str:
    """评论与概览共用的单行结论：符号 + 结论 + 合并建议。

    失败/用户停止时不能再输出通用的证据缺口结论，否则复制内容会被误读为
    一次完成的 PR Review；这里显式标注为部分审查记录。
    """
    kind = plan.outcome.kind
    if kind == "failed":
        return "❌ 结论：审查中断，尚无完整结论（部分审查记录）"
    if kind == "cancelled":
        return "⏹ 结论：审查已停止，已保留部分结果（部分审查记录）"
    label, _ = merge_heading(plan)
    symbol = MERGE_SYMBOLS.get(plan.digest.merge_decision.status, "❓")
    return f"{symbol} 结论：{label}"


def partial_review_note(plan) -> str:
    """失败/用户停止后复制的内容必须自述为部分记录，而不是正常完成的审查结论。"""
    if plan.outcome.kind == "failed":
        return "> 以下为执行失败前已保留的部分审查记录，不是完整审查结论；未完成范围不代表代码安全。"
    if plan.outcome.kind == "cancelled":
        return "> 以下为用户停止前已保留的部分审查记录，不是完整审查结论；未完成范围不代表代码安全。"
    return ""


def merge_advice(plan):
    label, advice = merge_heading(plan)
    decision = plan.digest.merge_decision
    lines = [f"**{label}**", "", advice]
    if decision.status != "unavailable" or decision.blocking_finding_keys or decision.nonblocking_finding_keys:
        lines.append(
            f"当前已确认：高优先级问题 {len(decision.blocking_finding_keys)} 项，"
            f"非高优先级建议 {len(decision.nonblocking_finding_keys)} 项。"
            "严重度映射不替代对部署条件和业务影响的判断，仍需满足项目要求的 CI 与必要审批。"
            "上述数量与建议由 RepoGuardian 审查策略生成，不代表 GitHub Mergeability 或分支保护结论。")
    if decision.limits:
        lines += ["", "仍需解决：", *[f"- {markdown_text(value)}" for value in decision.limits]]
    return "\n\n".join(lines)


def _author_goal(plan):
    understanding = plan.digest.change_understanding
    text = (understanding.author_goal if understanding else plan.digest.author_goal) or ""
    return author_goal_text(text)


def _author_source_lines(plan):
    lines = []
    if _author_goal(plan) not in {"", "未记录明确的作者目标。"}:
        lines.append("PR 目标来自作者提供的 PR 描述或标题，以本轮保存的内容为准；仅作审查背景，不代表目标已实现。")
    if plan.digest.author_context_limitation:
        lines.append("作者目标输入限制：" + markdown_text(plan.digest.author_context_limitation))
    return lines


def _change_understanding(plan, *, full=False):
    from app.services.publication.review_presentation import _change_overview

    understanding = plan.digest.change_understanding
    author_goal = _author_goal(plan) or "未记录明确的作者目标。"
    actual = ((understanding.implementation_changes or understanding.diff_summary
               or understanding.actual_changes) if understanding else "未保存固定 Diff 事实。")
    alignment = ("尚未形成完整的行为核验结论，不能判断作者目标是否实现。"
                 if plan.outcome.review_completeness != "complete"
                 else "源码范围检查已完成；覆盖记录不等于对作者全部目标的实现证明。")
    actual = _change_overview(plan.presentation) if plan.presentation else markdown_text(actual, 2000)
    lines = ["", "## PR 变更理解", "",
        "**PR 目标：** " + markdown_text(author_goal, 1200 if full else 400), "",
        "**变更概述：** " + actual, "",
        "**吻合与未确认：** " + markdown_text(alignment, 1600 if full else 500)]
    if plan.presentation:
        if plan.presentation.review_summary:
            lines += ["", "**审查综述：** " + markdown_text(plan.presentation.review_summary, 6000)]
        if plan.presentation.action_summary:
            lines += ["", "**行动说明：** " + markdown_text(plan.presentation.action_summary, 6000)]
        synthesis = plan.presentation.synthesis
        if plan.presentation.synthesis_status != "ready":
            lines += ["", "变更总览未生成；以上为已保存观察或变更范围。"]
        if full and synthesis:
            lines += ["", f"综合状态：{synthesis.status}；材料版本：`{synthesis.material_version}`；"
                f"原因：{markdown_text(synthesis.error or '无')}；模型请求：{synthesis.model_calls}。",
                "综合材料省略：" + markdown_text(str(synthesis.omitted))]
        if full:
            for observation in plan.presentation.observations:
                lines += ["", f"- 原始观察（不代表核验结论）`{observation.id}`："
                          + markdown_text(observation.change)]
                lines += [f"  - {markdown_text(ref.path)}:{ref.start_line}–{ref.end_line}"
                          f"（{ref.side.upper()}，证据 `{ref.id}`）" for ref in observation.sources]
    if full and plan.digest.actual_changes:
        lines += ["", "**变更文件（固定 Diff 事实）：**", "",
                  *[f"- {markdown_text(item, 1000)}" for item in plan.digest.actual_changes]]
    return lines


def _human_lines(view):
    if not view.disposition:
        return []
    label = {"open": "重新打开", "confirmed": "人工确认", "ignored": "已忽略", "resolved": "已处理"}[view.disposition.status]
    return [f"- 人工处理：{label}；原因：{markdown_text(view.disposition.reason)}；"
        f"时间：{markdown_text(view.updated_at.isoformat() if view.updated_at else '未记录')}。"]


def _report_finding_lines(plan, finding, view):
    lines = ["", "---", "", f"### {finding_markdown_title(finding)}", "", finding_location(plan, finding), "",
             render_finding(finding)]
    if finding.evidence_summary:
        lines += ["", "**核验理由：** " + markdown_text(finding.evidence_summary, 4000)]
    lines += _human_lines(view) if view else []
    return lines


def _appendix_lines(plan, task):
    outcome = plan.outcome
    lines = ["", "## 附录：审查范围与限制", "",
        f"- 审查目标完成：{outcome.examined_goals} / {outcome.total_goals}",
        f"- 本轮确认的问题（含归因单独标注的既有问题）：{outcome.confirmed_count}；未确认候选：{outcome.unresolved_count}",
        f"- 约定源码目标覆盖：{ {'complete': '完整', 'partial': '不完整', 'unknown': '未知'}[outcome.review_completeness] }；"
        f"运行可靠性：{ {'reliable': '可靠', 'unreliable': '存在限制', 'unknown': '未知'}[outcome.runtime_reliability] }",
        f"- 审查 Commit：{markdown_text(plan.snapshot.head_sha or '未记录', 64)}",
        f"- 审查记录：{markdown_text(plan.review_id, 160)}"]
    lines.append(f"- 模型：{markdown_text(task.configuration.provider)} / {markdown_text(task.configuration.model)}")
    if plan.digest.termination_reason:
        lines.append(f"- 终止记录：{markdown_text(plan.digest.termination_reason, 160)}")
    if plan.digest.actual_changes:
        lines += ["", "### 变更文件", "", *[f"- {markdown_text(item, 1000)}" for item in plan.digest.actual_changes]]
    lines += _goal_lines(plan.digest.examined_goals, "已检查目标", limit=None)
    lines += _goal_lines(plan.digest.incomplete_goals, "未完成目标", limit=None)
    if not plan.digest.examined_goals and not plan.digest.incomplete_goals:
        lines += ["", "缺少有效目标记录，不能判断检查范围。"]
    if plan.digest.excluded_files:
        lines += ["", "未纳入本轮审查的文件：", *[
            f"- {markdown_text(item.path, 500)}：{markdown_text(item.reason)}" for item in plan.digest.excluded_files]]
    lines += ["", "### 来源与验证边界", "", *_author_source_lines(plan), *_validation_lines(plan)]
    if outcome.reason_codes:
        lines += ["", "### 原因代码", "", *[f"- {REASON_LABELS[reason]}（{reason.value}）" for reason in outcome.reason_codes]]
    resolved = [item for item in outcome.review_limitations
                if item.status in {"resolved", "boundary", "nonblocking"}]
    if resolved:
        lines += ["", "### 已处理的审查限制", ""]
        for item in resolved:
            if item.status == "nonblocking":
                lines.append(f"- {markdown_text(item.text)}；处理结果：相关候选均不受此项阻塞。")
                for claim in item.resolution_claims:
                    label = "已解决" if claim.status == "resolved" else "不影响该候选"
                    lines.append(f"  - 候选 {markdown_text(claim.candidate_id[:12])}：{label}；"
                                 + markdown_text(claim.explanation))
                continue
            status = "已解决" if item.status == "resolved" else "一般审查边界"
            lines.append(f"- {markdown_text(item.text)}；处理结果：{status}；"
                         f"说明：{markdown_text(item.resolution.explanation)}；"
                         f"来源：{markdown_text(item.source_id[:12])}；"
                         f"复核记录：{markdown_text(item.resolved_by[:12])}")
    if plan.withheld_findings:
        lines += ["", "未进入发布正文的记录：", *[
            f"- {markdown_text(item.candidate_id, 160)}：{markdown_text(item.reason_code)}"
            for item in plan.withheld_findings]]
    lines += ["", "运行过程与证据追踪请查看技术审计记录；此 Markdown 不代表全部原始事件。"]
    return lines


def render_user_report(task, plan: PublicationPlan) -> str:
    """详细报告：结论、变更理解、问题与未决风险各出现一次，范围记录移入附录。"""
    outcome = plan.outcome
    label, advice = merge_heading(plan)
    lines = ["# RepoGuardian 详细审查报告", "", "## 审查结论", "", f"**{label}**", "", advice, "",
             outcome_description(plan), "",
             f"本轮确认的问题（含归因单独标注的既有问题） {outcome.confirmed_count} 项（其中 {outcome.publishable_count} 项形成可发布意见）；"
             f"未确认候选 {outcome.unresolved_count} 项。"]
    if plan.digest.merge_decision.limits:
        lines += ["", "需要补充或确认：",
                  *[f"- {markdown_text(value)}" for value in plan.digest.merge_decision.limits]]
    if task.review_report:
        roles = {item.id: {"reviewer": "主审", "contract": "契约审查",
                          "specialist": "风险专家审查"}[item.reviewer]
                 for item in task.review_report.assignments}
        failures = [item for item in task.review_report.discovery_results + task.review_report.verification_results
                    if item.status == "failed"]
        if failures:
            lines += ["", "**执行失败原因：**", ""]
            for item in failures:
                lines.append(f"- {roles.get(item.id, '独立核验或定向调查')}："
                    f"{execution_error_text(item.error)}（{markdown_text(item.error or 'unknown')}）；"
                    f"模型调用 {item.model_calls if item.model_calls is not None else '未知'} / "
                    f"{task.configuration.limits.model_calls}，读取或工具失败 {item.tool_failures} 次。")
    lines += _change_understanding(plan, full=True)
    lines += ["", "## 代码证据已确认的问题", ""]
    findings = _ordered_findings(plan)
    views = {item.id: item for item in plan.digest.findings}
    if not findings:
        lines.append("本轮没有通过发布准入、具备代码证据的问题；未确认候选和证据缺口见下方“未决风险”。")
    for finding in findings:
        lines += _report_finding_lines(plan, finding, views.get(finding.finding_key))
    published_keys = {item.finding_key for item in findings}
    for heading, states in (("已确认但暂不能发布的代码问题", {"confirmed"}),
                            ("需要人工确认的疑点（尚未确认缺陷）", {"candidate", "unresolved", "unknown"}),
                            ("已排除的候选", {"dismissed"})):
        candidates = [item for item in plan.digest.findings if item.id not in published_keys and item.review_status in states]
        if candidates:
            lines += ["", f"## {heading}", ""]
        for view in candidates:
            lines += [f"### {markdown_text(view.title or '未命名记录')}", "",
                f"- AI 判定：{ {'confirmed': '代码问题已确认，但暂不能生成可发布评论', 'candidate': '候选', 'unresolved': '尚未确认', 'dismissed': '已排除', 'unknown': '未知'}[view.review_status]}",
                f"- 严重程度：{markdown_text(view.severity or '未知')}",
                f"- 位置：{markdown_text(view.location.path + ':' + str(view.location.start_line)) + '（' + view.location.side + '）' if view.location else '尚无可靠代码定位'}"]
            for field, value in (("影响", view.impact), ("触发场景", view.message), ("建议", view.recommendation)):
                if value:
                    lines.append(f"- {field}：{markdown_text(value)}")
            lines += _human_lines(view)
            if view.review_status != "confirmed":
                lines.append("以上为候选或排除记录，不能作为已核验的发布评论。")
    lines += _risk_section(plan)
    lines += _next_steps(plan)
    lines += discussion_report_lines(plan, task)
    lines += _appendix_lines(plan, task)
    return "\n".join(lines)


def discussion_report_lines(plan, task=None):
    if plan.digest.discussion_review_enabled is False:
        return ["", "## PR 讨论上下文", "", "本次未纳入 PR 讨论上下文。"]
    stats = plan.digest.discussion_stats
    if stats is None:
        return ["", "## PR 讨论上下文", "", "未记录讨论采集统计，不能推断该 PR 没有讨论。"]
    status = "完整" if stats["status"] == "complete" else "不完整"
    lines = ["", "## PR 讨论上下文", "",
        f"采集状态：{status}；已保存 {stats['collected']} 项讨论供 Agent 按需读取。",
        "讨论仅作为背景，不代表相关主张已核实；源码核验结果统一记录为当前候选和问题。"]
    lines += [f"- 限制：{markdown_text(value)}" for value in stats["limitations"]]
    return lines


def _risk_section(plan):
    outcome = plan.outcome
    lines = ["", "## 未决风险", ""]
    items = []
    for reason in outcome.reason_codes:
        items.append(f"- {REASON_LABELS[reason]}")
    items.extend(f"- {markdown_text(review_limitation_text(item))}"
                 for item in outcome.review_limitations if item.status in {"pending", "unresolved"})
    for goal in plan.digest.incomplete_goals[:5]:
        items.append(f"- 未完成目标：{markdown_text(goal.path or '文件未记录', 500)}"
                     + (f":{goal.start_line}–{goal.end_line}" if goal.start_line else "")
                     + (f"（{goal.side.upper()}）" if goal.side else "")
                     + (f"；{markdown_text(goal.reason)}" if goal.reason else ""))
    if not items:
        lines.append("没有需要单独关注的未决风险；仍以审查范围与验证边界为准。")
    else:
        lines += items
    return lines


def _next_steps(plan):
    outcome = plan.outcome
    lines = ["", "## 下一步建议", ""]
    steps = []
    if plan.digest.merge_decision.blocking_finding_keys:
        steps.append("- 修复上方高优先级问题并在新快照重新核验；人工标记已处理不能代替机器复核。")
    if plan.digest.merge_decision.nonblocking_finding_keys:
        steps.append("- 逐项评估非高优先级建议的影响与处理方式。")
    if plan.digest.incomplete_goals:
        steps.append("- 补查未完成目标，再判断整轮审查是否完整。")
    if outcome.incomplete_investigations:
        steps.append("- 补齐列出的必要证据，或明确其一般边界归类；源码目标检查完成不代表审查限制已解决。")
    if outcome.pending_limitation_count:
        steps.append("- 完成限制声明的必要性复核；尚未复核的声明不能当作已确认的证据缺口。")
    if outcome.unresolved_count:
        steps.append("- 在问题与证据页核对未决候选，补齐证据后再决定是否作为缺陷处理。")
    if not steps:
        advice = plan.digest.merge_decision.advice if plan.digest.merge_decision.status == "resolve_unknowns" else ""
        steps.append("- " + (advice or "结合项目自身的测试、CI 和人工判断决定是否合并；本轮结论不替代审批。"))
    lines += steps
    return lines




def _comment_finding_lines(plan, finding, view, *, full):
    """按对外评论结构渲染单条问题：等级、标题、位置、影响、建议。"""
    severity = finding.severity.title()
    category = f" · {markdown_text(finding.category.title(), 100)}" if finding.category else ""
    # 等级与标题同一行，与对外评论示例一致；位置紧随其后。
    lines = [f"**[{severity}{category}] {finding_markdown_title(finding)}**", "",
             _comment_location(plan, finding), ""]
    impact = _sentence(finding.affected_behavior)
    if impact:
        lines += [markdown_text(impact), ""]
    if full:
        if finding.evidence_summary:
            lines += [f"**代码证据：** {markdown_text(finding.evidence_summary)}", ""]
        if finding.failure_scenario:
            lines += [f"**触发场景：** {markdown_text(finding.failure_scenario)}", ""]
    if finding.recommendation:
        lines += [f"**建议：** {markdown_text(_sentence(finding.recommendation, 320))}", ""]
    # 评论是交付文档；人工处理状态单独成行，但不重复免责声明。
    lines += _human_lines(view) if view else []
    return lines


def _comment_location(plan, finding) -> str:
    """评论正文中的位置行；有严格解析的 PR 身份时使用不可变快照链接。"""
    label = _finding_location_text(finding)
    href = _location_href(plan, finding)
    return f"[{label}]({href})" if href else label


def _comment_change_overview(plan, *, title="变更理解"):
    """分别展示作者声明和绑定证据的代码观察。"""
    understanding = plan.digest.change_understanding
    author = _author_goal(plan)
    actual = understanding.actual_changes if understanding else ""
    lines = [f"## {title}", ""]
    if author and author != "未记录明确的作者目标。":
        source = understanding.author_goal_source if understanding else None
        label = {"title": "作者标题，未生成目标归纳", "description": "作者描述摘录",
                 "summary": "基于作者原文的目标归纳"}.get(source, "作者声明")
        note = "" if source == "summary" else f"（{label}，不代表目标已实现）"
        lines += [f"**PR 目标：** {markdown_text(_sentence(author, 360))}{note}", ""]
    if understanding and understanding.diff_summary:
        lines += [f"**变更范围（Diff 元数据）：** {markdown_text(_sentence(understanding.diff_summary, 600))}", ""]
    if understanding and understanding.implementation_changes is not None:
        lines += [f"**实现变化：** {markdown_text(understanding.implementation_changes, 800)}", "",
                  f"**测试核对：** {markdown_text(understanding.test_observations or '尚未形成测试断言核对记录。', 800)}"
                  "（仅指测试代码静态核对，实际执行情况见文末。）"]
    else:
        lines += [f"**代码观察：** {markdown_text(_sentence(actual or '未保存可归纳的实际变更。', 600))}"]
    return lines


def _comment_other_results(plan, *, omitted, retained, withheld, full, title=None, extra_lines=()):
    """只展开实际缺口；通用验证边界统一放在文末。

    章节标题按内容决定：确实存在未确认事项时使用“仍需确认的事项”；
    只有完整版的审查范围、来源等补充信息时使用“审查范围与补充信息”，
    不让完整明细本身制造一个并不存在的风险章节。简洁版与完整版共享同一
    判断结果，完整版只追加明细，不改变章节是否存在。
    """
    risks = _comment_risks(plan)
    entries = list(risks)
    if plan.digest.incomplete_goals:
        entries.append(f"- 约定目标仍有 {len(plan.digest.incomplete_goals)} 项未完成检查。")
        entries += _goal_lines(plan.digest.incomplete_goals, "待补查目标", limit=3)
    if plan.outcome.unresolved_count:
        entries.append(f"- 另有 {plan.outcome.unresolved_count} 项候选问题尚未确认，不作为已核验缺陷。")
    if omitted:
        entries.append("**本次未包含的事项（人工标记已处理，不代表机器重新核验通过）：**")
        entries += [f"- [{item.severity}] {markdown_text(item.title)}；{_finding_location_text(item)}"
                    for item in omitted]
    if retained:
        entries.append(f"- {len(retained)} 项已标记处理的高优先级问题仍保留正文，需在新快照重新核验。")
    if withheld:
        entries.append(f"- 另有 {withheld} 项代码证据已确认的记录未通过发布准入，详见完整报告；"
                       "当前评论未将其描述为已核验问题。")
    has_pending = bool(entries)
    if full:
        source = _author_source_lines(plan)
        if source:
            entries += ["", "### 来源与验证边界", "", *source]
    elif plan.digest.author_context_limitation:
        limitation = markdown_text(plan.digest.author_context_limitation)
        if not any(limitation in entry for entry in entries):
            entries.append("- 作者目标输入限制：" + limitation)
            has_pending = True
    entries += extra_lines
    if not entries:
        return []
    heading = title or ("仍需确认的事项" if has_pending else "审查范围与补充信息")
    return ["", f"## {heading}", "", *entries]




def comment_selection(plan, *, exclude_resolved=False):
    findings = _ordered_findings(plan)
    resolved = {item.id for item in plan.digest.findings if item.disposition and item.disposition.status == "resolved"}
    blockers = set(plan.digest.merge_decision.blocking_finding_keys)
    # 行内发布的默认选择不决定完整评论是否披露当前风险。
    omitted = [item for item in findings if exclude_resolved and item.finding_key in resolved
               and item.severity in {"medium", "low"} and item.finding_key not in blockers]
    omitted_keys = {item.finding_key for item in omitted}
    included = [item for item in findings if item.finding_key not in omitted_keys]
    retained = [item.finding_key for item in included if exclude_resolved and item.finding_key in resolved
                and (item.finding_key in blockers or item.severity in {"critical", "high"})]
    return included, omitted, retained


def _comment_next_steps(plan, *, has_findings: bool):
    """面向作者的单段行动建议，避免把报告中的多条清单原样搬进评论。"""
    decision = plan.digest.merge_decision
    steps = []
    if plan.outcome.kind == "failed":
        return ["审查因执行错误中断，以下建议不构成完整审查结论；请处理运行故障后重新审查。"]
    if plan.outcome.kind == "cancelled":
        return ["审查已由用户停止，以下建议仅基于停止前保留的记录；请补查剩余范围后重新审查。"]
    if decision.status == "fix_blockers":
        steps.append("先修复上述高优先级问题，并在新快照重新核验后再考虑合并。")
    elif decision.status == "resolve_unknowns":
        steps.append(decision.advice or "先核对限制说明中的事项及原因，再判断是否可以合并。")
    elif has_findings:
        steps.append("请评估上述非高优先级问题的业务影响，结合 CI 与必要审批后再决定合并。")
    else:
        steps.append("结合 CI、业务预期和人工评审决定是否合并。")
    if plan.digest.merge_decision.nonblocking_finding_keys and decision.status == "fix_blockers":
        steps.append("非高优先级建议可在同一轮修改中一并评估。")
    if plan.digest.incomplete_goals:
        steps.append("未完成的约定目标需要补查后才能判断整轮审查是否完整。")
    return [" ".join(steps)]


def render_review_comment(plan, *, detail="compact", exclude_resolved=False):
    """固定章节语义，只按风险调整顺序；没有内容的章节不输出。

    章节含义在所有状态下一致：结论、本次变更理解、需要处理的问题、其他建议、
    仍需确认的事项、下一步、快照与验证边界。这样不同 PR 复制出的评论遵循同一套阅读顺序。
    """
    if plan.presentation is not None:
        from app.services.publication.review_presentation import render_presentation
        return render_presentation(plan, copy=True, detail=detail, exclude_resolved=exclude_resolved)
    included, omitted, retained = comment_selection(plan, exclude_resolved=exclude_resolved)
    full = detail == "full"
    withheld = max(0, plan.outcome.confirmed_count - plan.outcome.publishable_count)
    lines = ["RepoGuardian · PR Code Review", "", merge_verdict_line(plan), ""]
    if plan.digest.discussion_review_enabled is False:
        lines += ["本次未纳入历史讨论；以下结论不包含历史讨论复核。", ""]
    if plan.digest.discussion_stats:
        stats = plan.digest.discussion_stats
        lines += [f"已保存 {stats['collected']} 项 PR 讨论供 Agent 按需读取；采集数量不代表已核验数量。", ""]
    note = partial_review_note(plan)
    if note:
        lines += [note, ""]
    interrupted = plan.outcome.kind in {"failed", "cancelled"}
    blockers = set(plan.digest.merge_decision.blocking_finding_keys)
    blocking_findings = [item for item in included if item.severity in {"high", "critical"} or item.finding_key in blockers]
    blocking_keys = {item.finding_key for item in blocking_findings}
    suggestions = [item for item in included if item.finding_key not in blocking_keys]
    incomplete = (plan.outcome.kind == "inconclusive"
        or plan.outcome.review_completeness != "complete"
        or plan.outcome.runtime_reliability == "unreliable"
        or plan.digest.merge_decision.status == "resolve_unknowns")
    views = {item.id: item for item in plan.digest.findings}

    def finding_section(findings, title):
        # 只有实际条目才输出章节，避免出现“建议”标题后只有行动文案的空章节。
        if not findings:
            return []
        result = ["", f"## {title}", ""]
        for finding in findings:
            needs_evidence = (full or finding.severity in {"high", "critical"}
                or finding.finding_key in blockers)
            result += _comment_finding_lines(plan, finding, views.get(finding.finding_key), full=needs_evidence)
        return result

    scope_lines = []
    if full:
        scope_lines += ["", "### 审查范围", "",
            f"约定目标已检查 {plan.outcome.examined_goals} / {plan.outcome.total_goals}；"
            f"未决候选 {plan.outcome.unresolved_count} 项；未解决审查限制 {plan.outcome.incomplete_investigations} 项。"]
        scope_lines += _goal_lines(plan.digest.examined_goals, "已检查目标", limit=None)
        scope_lines += _goal_lines(plan.digest.incomplete_goals, "未完成目标", limit=None)
        scope_lines += [f"- 排除文件：{markdown_text(item.path)}；{markdown_text(item.reason)}" for item in plan.digest.excluded_files]

    def limitations():
        return _comment_other_results(plan, omitted=omitted, retained=retained, withheld=withheld,
            full=full, extra_lines=scope_lines)

    def next_steps_section():
        return ["", "## 下一步", "", *_comment_next_steps(plan, has_findings=bool(included))]

    def understanding_section():
        return ["", *_comment_change_overview(plan)]

    if interrupted:
        lines += understanding_section()
        scope = (f"约定目标已检查 {plan.outcome.examined_goals} / {plan.outcome.total_goals} 项"
                 if plan.outcome.total_goals else "约定目标的检查范围未知")
        lines += ["", scope + "；以上仅为已保存的检查观察，不代表完整结论。"]
        lines += limitations()
        lines += next_steps_section()
    elif blocking_findings:
        lines += finding_section(blocking_findings, "需要处理的问题")
        lines += understanding_section()
        lines += finding_section(suggestions, "其他建议")
        lines += limitations()
        lines += next_steps_section()
    elif suggestions or omitted:
        lines += understanding_section()
        lines += finding_section(suggestions, "其他建议")
        lines += limitations()
        lines += next_steps_section()
    elif incomplete:
        lines += understanding_section()
        lines += ["", "> 以上变更理解仅基于已保存的局部观察或变更元数据，"
                  "不代表所有目标已完成核验。"]
        lines += limitations() or [
            "", "## 仍需确认的事项", "", "缺口或运行记录尚不明确，请先核对详细报告。"]
        lines += next_steps_section()
    else:
        lines += understanding_section()
        lines += limitations()
        lines += next_steps_section()
    validation = STATIC_BOUNDARY
    boundary = "" if any(validation in line for line in lines) else validation
    subject = "本记录" if note else "本结论"
    lines += ["", "---", "",
        f"{subject}基于提交 `{markdown_text(plan.snapshot.head_sha or '未记录', 64)}` 的只读审查；"
        + boundary + "未发现问题不等于代码无缺陷；不替代 CI 与人工审批。"]
    # 各段落自行收尾空行；合并后收敛多余空行，保持可发布排版。
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines))


def render_finding_comment(plan, finding):
    lines = [finding_location(plan, finding), "", finding.body_markdown]
    view = next((item for item in plan.digest.findings if item.id == finding.finding_key), None)
    if view:
        lines += _human_lines(view)
    other_count = len(_ordered_findings(plan)) - 1
    lines += ["", f"本评论仅针对当前选中的一个问题；另有 {other_count} 项已准入问题未包含在本条评论中。"
        "完整审查结论、被扣留记录与未决风险请查看完整 PR 评论和详细报告。", "",
        f"审查 Commit：`{markdown_text(plan.snapshot.head_sha or '未记录', 64)}`；只读静态审查，未执行目标代码、测试或构建。"]
    return "\n".join(lines)


def render_summary(plan, *, discussion_urls: dict[str, str] | None = None) -> str:
    """GitHub 交付摘要：复用评论措辞，运行与证据明细留在内部报告。"""
    if plan.presentation is not None:
        from app.services.publication.review_presentation import render_presentation
        return render_presentation(plan, discussion_urls=discussion_urls)
    outcome = plan.outcome
    digest = plan.digest
    lines = ["## RepoGuardian · PR Review", "", merge_verdict_line(plan), ""]
    if digest.discussion_stats:
        stats = digest.discussion_stats
        lines += [f"已保存 {stats['collected']} 项 PR 讨论供 Agent 按需读取；采集数量不代表已核验数量。", ""]
    note = partial_review_note(plan)
    if note:
        lines += [note, ""]
    lines += [f"本轮确认的问题（含归因单独标注的既有问题）：{outcome.confirmed_count}；"
              f"约定目标已检查 {outcome.examined_goals} / {outcome.total_goals} 项。"]
    findings = _ordered_findings(plan)
    if findings:
        lines += ["", "### 问题索引", ""]
        for index, finding in enumerate(findings, 1):
            location = _finding_location_text(finding)
            discussion = (discussion_urls or {}).get(finding.finding_key) if finding.placement == "inline" else None
            lines.append(f"{index}. [{finding.severity.title()}] {finding_markdown_title(finding)} — {location}"
                         + ("（详见下方）" if finding.placement == "summary" else "")
                         + (f" · [查看 Inline 讨论]({discussion})" if discussion else ""))
        for finding in plan.summary_findings:
            lines += ["", render_finding(finding)]
    risks = _comment_risks(plan)
    if digest.incomplete_goals:
        remaining = len(digest.incomplete_goals)
        risks.append(f"- {remaining} 项约定目标未完成检查；具体范围请查看 RepoGuardian 详细报告。")
    if digest.excluded_files:
        risks.append(f"- {len(digest.excluded_files)} 个文件未纳入本轮审查；具体范围与原因请查看 RepoGuardian 详细报告。")
    if outcome.unresolved_count:
        risks.append(f"- {outcome.unresolved_count} 项候选问题尚未确认，不作为已核验缺陷。")
    withheld_count = max(0, outcome.confirmed_count - outcome.publishable_count)
    if withheld_count > 0:
        risks.append(f"- {withheld_count} 项代码证据已确认的记录未进入发布草稿；请在 RepoGuardian 中核对证据与原因。")
    if risks:
        lines += ["", "### 仍需关注", "", *dict.fromkeys(risks)]
    if digest.author_context_limitation and not any(digest.author_context_limitation in line for line in risks):
        lines += ["", "作者目标输入限制：" + markdown_text(digest.author_context_limitation)]
    lines += ["", "### 下一步", "", *_comment_next_steps(plan, has_findings=bool(findings))]
    validation = STATIC_BOUNDARY
    boundary = "" if any(validation in line for line in lines) else validation
    lines += ["", "---", "", f"审查 Commit：`{markdown_text(plan.snapshot.head_sha or '未记录', 64)}`；"
              + boundary + "本结果不替代 CI 与人工审批。"]
    return "\n".join(lines)
