"""仅渲染后端已准入文本；不访问 GitHub、模型或仓库。"""

import html
import re
import unicodedata
from urllib.parse import quote

from app.models.publication import PublicationFinding, PublicationPlan


OUTCOME_LABELS = {"clean": "未发现可行动问题", "findings": "发现已确认问题", "inconclusive": "本次审查未得出完整结论",
                  "failed": "审查失败", "cancelled": "审查已取消"}
OUTCOME_DESCRIPTIONS = {
    "clean": "已完成本次约定范围的静态审查，未发现具有充分代码证据、需要 PR 作者处理的问题。未发现问题不等于证明代码正确。",
    "findings": "请逐项核对下列已确认问题及建议；仍需关注的限制见下方说明。",
    "inconclusive": "目前没有可确认的问题，但审查仍存在缺口或可靠性限制，不能据此认定本次变更没有问题。",
    "failed": "审查执行失败，请处理运行故障并重新审查；已有记录不能视为完整结论。",
    "cancelled": "审查已取消，请补查剩余范围后再判断本次变更。",
}
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
MERGE_LABELS = {
    "fix_blockers": "建议修改后再合并",
    "resolve_unknowns": "需要补充检查",
    "review_suggestions": "建议评估后合并",
    "conditional_merge": "未发现阻断性问题",
    "unavailable": "需要补充检查",
}
LIMIT_LABELS = {
    "author_context_truncated": "作者声明输入已截断，未输入部分的目标尚未核验",
    "author_context_unknown": "作者声明输入是否完整未知，目标范围仍需确认",
    "finding_severity_unknown": "存在严重度未知的问题记录",
    "author_intent_unresolved": "作者目标仍有未核验路径或矛盾",
    "head_validation_failed": "当前 Head 的已有验证记录为失败",
    "head_validation_unknown": "已有验证记录无法确定当前 Head 结果",
    "confirmed_findings_withheld": "仍有未通过发布准入的机器确认记录",
}


def markdown_text(value: str, limit: int = 2000) -> str:
    text = " ".join("".join(char for char in value if not unicodedata.category(char).startswith("C") or char in "\n\t").split())
    text = text[:limit] + ("…" if len(text) > limit else "")
    return re.sub(r"([\\`*_{}\[\]()#+.!|~>-])", r"\\\1", html.escape(text, quote=True)).replace("@", "&#64;")


def _sentence(value: str | None, limit: int = 240) -> str:
    """取一段可读文本的首句，避免评论被整段模型输出淹没。"""
    text = " ".join((value or "").split())
    if not text:
        return ""
    match = re.match(r".+?[。！？!?](?=\s|$)|.+", text)
    return match.group(0)[:limit] if match else text[:limit]


def render_finding(finding: PublicationFinding) -> str:
    """正文已由 Planner 从 verified publication 文本生成，不再回退候选。"""
    return finding.body_markdown


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
    lines = [f"审查完整性：{completeness[plan.outcome.review_completeness]}；"
            f"运行可靠性：{reliability[plan.outcome.runtime_reliability]}。",
        "仅完成只读静态审查；未执行目标仓库代码、测试或构建。未发现问题不等于证明代码正确。"
        if plan.digest.validation_state == "not_executed" else
        {"passed": "已有与当前 Head 绑定的受控验证通过记录；仅适用于记录的检查，不代替完整 CI 或审批。",
         "failed": "已有与当前 Head 绑定的验证失败记录，需核对失败原因。",
         "independent_results": "已有独立补丁验证结果；补丁通过不代表当前 PR Head 已通过测试。",
         "unknown": "动态验证状态未知；不能据此宣称目标仓库测试或构建已通过。"}[plan.digest.validation_state]]
    for result in plan.digest.validations:
        lines.append(f"- {markdown_text(result.source)} / {markdown_text(result.stage)}：{markdown_text(result.status)}；"
            f"可信绑定：{'是' if result.trusted and result.snapshot_matches else '否'}。{markdown_text(result.reason)}")
    return lines


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
        status = {"examined": "已检查", "not_examined": "未检查", "unsupported": "暂不支持", "unknown": "未知"}.get(goal.status, goal.status)
        lines.append(f"- {location}：{markdown_text(status, 100)}{reason}")
    if limit is not None and len(goals) > limit:
        lines.append(f"另有 {len(goals) - limit} 项，完整目标见用户报告。")
    return lines


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


def finding_location(plan, finding):
    anchor = finding.anchor
    label = _finding_location_text(finding)
    # 仅从严格解析的 GitHub PR 身份生成链接，不接受仓库提供的 URL。
    from app.tools.github_tool import parse_pr_url
    try:
        owner, repo, _ = parse_pr_url(plan.digest.pr_url or "")
    except ValueError:
        owner = repo = None
    sha = plan.snapshot.effective_base_sha if anchor.side == "base" else plan.snapshot.head_sha
    if owner and repo and sha and re.fullmatch(r"[a-f0-9]{40,64}", sha) and anchor.start_line and anchor.side:
        url = f"https://github.com/{quote(owner, safe='')}/{quote(repo, safe='')}/blob/{sha}/{quote(anchor.file_path, safe='/')}#L{anchor.start_line}"
        return f"**代码位置：** [{label}]({url})"
    return f"**代码位置：** {label}"


def merge_heading(plan):
    decision = plan.digest.merge_decision
    return MERGE_LABELS.get(decision.status, decision.label), decision.advice


def merge_limits(plan):
    decision = plan.digest.merge_decision
    return [LIMIT_LABELS.get(reason, REASON_LABELS.get(reason, reason)) for reason in decision.unresolved_reasons]


def merge_advice(plan):
    label, advice = merge_heading(plan)
    decision = plan.digest.merge_decision
    lines = [f"**{label}**", "", advice,
        f"阻断问题 {len(decision.blocking_finding_keys)} 项；非阻断建议 {len(decision.nonblocking_finding_keys)} 项。"
        "严重度映射不替代对部署条件和业务影响的判断，仍需满足项目要求的 CI 与必要审批。"]
    if decision.limits:
        lines += ["", "仍需解决：", *[f"- {value}" for value in decision.limits]]
    return "\n\n".join(lines)


def _change_understanding(plan, *, full=False):
    understanding = plan.digest.change_understanding
    author_goal = understanding.author_goal if understanding else (plan.digest.author_goal or "未记录作者声明")
    actual = understanding.actual_changes if understanding else "未保存可归纳的实际变更。"
    alignment = understanding.alignment if understanding else plan.digest.intent_alignment
    lines = ["", "## PR 变更理解", "",
        "**作者想解决什么：** " + markdown_text(author_goal, 1200 if full else 400), "",
        "**代码实际改变了什么：** " + markdown_text(actual, 2000 if full else 600), "",
        "**吻合与未确认：** " + markdown_text(alignment, 1600 if full else 500)]
    if plan.digest.author_context_limitation:
        lines += ["", "**作者目标输入限制：** " + markdown_text(plan.digest.author_context_limitation)]
    if full and plan.digest.actual_changes:
        lines += ["", "**变更文件（固定 Diff 事实）：**", "",
                  *[f"- {markdown_text(item, 1000)}" for item in plan.digest.actual_changes]]
    if full:
        lines += _intent_detail(plan)
    return lines


def _intent_detail(plan):
    lines = []
    labels = {"observed": "局部静态证据支持", "contradicted": "存在静态证据矛盾", "insufficient": "证据不足",
              "not_applicable": "当前检查不适用", "not_checked": "尚未核验"}
    for item in plan.digest.intent_assessments:
        lines += ["", f"- 作者目标：{markdown_text(item.author_target, 4000)}；判断：{labels[item.status]}。"]
        for observation in item.observations:
            lines.append(f"  - 实际行为：{markdown_text(observation['actual_behavior'])}；"
                f"位置：{markdown_text(str(observation.get('path') or '未知文件'))}:{observation.get('start_line') or '未知'}"
                f"（{observation['side']}）；证据：{markdown_text('、'.join(observation['evidence_ids']) or '未获得有效证据')}。")
            lines += ["  - 限制：" + markdown_text(value) for value in observation["limitations"]]
    return lines


def _human_lines(view):
    if not view.disposition:
        return []
    label = {"open": "重新打开", "confirmed": "人工确认", "ignored": "已忽略", "resolved": "已处理"}[view.disposition.status]
    return [f"- 人工处理：{label}；原因：{markdown_text(view.disposition.reason)}；"
        f"时间：{markdown_text(view.updated_at.isoformat() if view.updated_at else '未记录')}。"]


def _report_finding_lines(plan, finding, view):
    lines = ["", "---", "", f"### {markdown_text(finding.title)}", "", finding_location(plan, finding), "",
             render_finding(finding)]
    lines += _human_lines(view) if view else []
    return lines


def _appendix_lines(plan, task):
    outcome = plan.outcome
    lines = ["", "## 附录：审查范围与限制", "",
        f"- 审查目标完成：{outcome.examined_goals} / {outcome.total_goals}",
        f"- 已确认问题：{outcome.confirmed_count}；未决候选：{outcome.unresolved_count}",
        f"- 审查完整性：{ {'complete': '完整', 'partial': '不完整', 'unknown': '未知'}[outcome.review_completeness] }；"
        f"运行可靠性：{ {'reliable': '可靠', 'unreliable': '存在限制', 'unknown': '未知'}[outcome.runtime_reliability] }",
        f"- 审查 Commit：{markdown_text(plan.snapshot.head_sha or '未记录', 64)}",
        f"- 审查记录：{markdown_text(plan.review_id, 160)}"]
    if task.run_manifest:
        lines.append(f"- 模型：{markdown_text(task.run_manifest.provider)} / {markdown_text(task.run_manifest.model)}")
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
    lines += ["", "### 验证边界", "", *_validation_lines(plan)]
    if outcome.reason_codes:
        lines += ["", "### 原因代码", "", *[f"- {REASON_LABELS[reason]}（{reason.value}）" for reason in outcome.reason_codes]]
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
             OUTCOME_DESCRIPTIONS[outcome.kind], "",
             f"已确认问题 {outcome.confirmed_count} 项（其中 {outcome.publishable_count} 项形成可发布意见）；"
             f"未决候选 {outcome.unresolved_count} 项。"]
    if plan.digest.merge_decision.limits:
        lines += ["", "需要补充或确认：", *[f"- {value}" for value in plan.digest.merge_decision.limits]]
    lines += _change_understanding(plan, full=True)
    lines += ["", "## 已确认问题", ""]
    findings = _ordered_findings(plan)
    views = {item.id: item for item in plan.digest.findings}
    if not findings:
        lines.append("当前没有通过发布准入的已核验问题。未决候选和证据缺口见下方“未决风险”。")
    for finding in findings:
        lines += _report_finding_lines(plan, finding, views.get(finding.finding_key))
    published_keys = {item.finding_key for item in findings}
    for heading, states in (("机器已确认但未形成发布意见", {"confirmed"}),
                            ("需要人工确认的疑点（尚未确认缺陷）", {"candidate", "unresolved", "unknown"}),
                            ("已排除的候选", {"dismissed"})):
        candidates = [item for item in plan.digest.findings if item.id not in published_keys and item.review_status in states]
        if candidates:
            lines += ["", f"## {heading}", ""]
        for view in candidates:
            lines += [f"### {markdown_text(view.title or '未命名记录')}", "",
                f"- AI 判定：{ {'confirmed': '机器已确认，发布准入未通过', 'candidate': '候选', 'unresolved': '尚未确认', 'dismissed': '已排除', 'unknown': '未知'}[view.review_status]}",
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
    lines += _appendix_lines(plan, task)
    return "\n".join(lines)


def _risk_section(plan):
    outcome = plan.outcome
    lines = ["", "## 未决风险", ""]
    items = []
    for reason in outcome.reason_codes:
        items.append(f"- {REASON_LABELS[reason]}")
    for goal in plan.digest.incomplete_goals[:5]:
        items.append(f"- 未完成目标：{markdown_text(goal.path or '文件未记录', 500)}"
                     + (f":{goal.start_line}" if goal.start_line else "")
                     + (f"；{markdown_text(goal.reason)}" if goal.reason else ""))
    pending = [item for item in plan.digest.investigations if item.answer == "unresolved"]
    for item in pending[:5]:
        items.append(f"- 未决调查：{markdown_text(item.proposition)}"
                     + (f"；{markdown_text(item.reason)}" if item.reason else "；尚未记录明确原因"))
    if plan.digest.unresolved_leads:
        items += [f"- 未调度线索：{markdown_text(value)}" for value in plan.digest.unresolved_leads[:5]]
    if not items:
        lines.append("没有需要单独关注的未决风险；仍以审查范围与验证边界为准。")
    else:
        lines += items
    if plan.digest.investigations_known and len(pending) > 5:
        lines.append(f"- 另有 {len(pending) - 5} 项未决调查，完整记录见技术审计。")
    answered = [item for item in plan.digest.investigations if item.answer != "unresolved"]
    if answered:
        lines += ["", "### 关键调查摘要", ""]
        for item in answered[:5]:
            label = "静态证据支持此命题" if item.answer == "supported" else "静态证据反驳此命题"
            lines.append(f"- {markdown_text(item.proposition)}：{label}"
                         + (f"；{markdown_text(item.reason)}" if item.reason else "")
                         + "".join(f"；限制：{markdown_text(value)}" for value in item.limitations))
        if len(answered) > 5:
            lines.append(f"- 另有 {len(answered) - 5} 项已回答调查，完整记录见技术审计。")
    return lines


def _next_steps(plan):
    outcome = plan.outcome
    lines = ["", "## 下一步建议", ""]
    steps = []
    if plan.digest.merge_decision.blocking_finding_keys:
        steps.append("- 修复上方阻断问题并在新快照重新核验；人工标记已处理不能代替机器复核。")
    if plan.digest.merge_decision.nonblocking_finding_keys:
        steps.append("- 逐项评估非阻断建议的影响与处理方式。")
    if plan.digest.incomplete_goals:
        steps.append("- 补查未完成目标，再判断整轮审查是否完整。")
    if outcome.incomplete_investigations:
        steps.append("- 处理未决调查；初始目标检查完成不代表调用链调查已经得到答案。")
    if outcome.unresolved_count:
        steps.append("- 在问题与证据页核对未决候选，补齐证据后再决定是否作为缺陷处理。")
    if not steps:
        steps.append("- 结合项目自身的测试、CI 和人工判断决定是否合并；本轮结论不替代审批。")
    lines += steps
    return lines


def _comment_risks(plan):
    """只列影响合并判断的限制；检查过程与完整目标清单留在详细报告。"""
    risks = list(plan.digest.merge_decision.limits)
    labels = {"contradicted": "存在静态证据矛盾", "insufficient": "证据不足", "not_checked": "尚未核验",
              "not_applicable": "当前检查不适用"}
    for item in plan.digest.intent_assessments:
        if item.status in {"contradicted", "insufficient", "not_checked"}:
            risks.append(f"作者目标“{_sentence(item.author_target, 120)}”{labels[item.status]}。")
        for observation in item.observations:
            risks += [f"限制：{value}" for value in observation.get("limitations") or []]
    pending = [item for item in plan.digest.investigations if item.answer == "unresolved"]
    if pending:
        risks.append(f"{len(pending)} 项调查未得到充分答案，相关调用链结论仍需补充证据。")
    if plan.digest.validation_state == "failed":
        risks.append("已有与当前 Head 绑定的验证失败记录，需核对失败原因。")
    elif plan.digest.validation_state == "unknown":
        risks.append("动态验证状态未知，不能据此宣称目标仓库测试或构建已通过。")
    return [f"- {value}" for value in dict.fromkeys(risks)][:6]


def _comment_finding_lines(plan, finding, index, view, *, full):
    severity = finding.severity.title()
    category = f" · {markdown_text(finding.category.title(), 100)}" if finding.category else ""
    lines = [f"**{index}. [{severity}{category}] {markdown_text(finding.title, 200)}**", "",
             finding_location(plan, finding), ""]
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


def comment_selection(plan, *, exclude_resolved=False):
    findings = _ordered_findings(plan)
    resolved = {item.id for item in plan.digest.findings if item.disposition and item.disposition.status == "resolved"}
    blockers = set(plan.digest.merge_decision.blocking_finding_keys)
    omitted = [item for item in findings if exclude_resolved and item.finding_key in resolved
               and item.severity in {"medium", "low"} and item.finding_key not in blockers]
    omitted_keys = {item.finding_key for item in omitted}
    included = [item for item in findings if item.finding_key not in omitted_keys]
    retained = [item.finding_key for item in included if exclude_resolved and item.finding_key in resolved
                and (item.finding_key in blockers or item.severity in {"critical", "high"})]
    return included, omitted, retained


def render_review_comment(plan, *, detail="compact", exclude_resolved=False):
    """默认评论是可直接发布的审查意见；full 只增加证据与范围明细。"""
    included, omitted, retained = comment_selection(plan, exclude_resolved=exclude_resolved)
    full = detail == "full"
    label, advice = merge_heading(plan)
    decision = plan.digest.merge_decision
    lines = ["## RepoGuardian · PR 审查评论", "", f"**审查结论：{label}**", "", advice, ""]
    if decision.blocking_finding_keys or decision.nonblocking_finding_keys:
        lines += [f"阻断问题 {len(decision.blocking_finding_keys)} 项；"
                  f"非阻断建议 {len(decision.nonblocking_finding_keys)} 项。"
                  "严重度映射不替代对部署条件和业务影响的判断，仍需满足 CI 与必要审批。"]
    lines += _change_understanding(plan)
    lines += ["", "## 发现的问题", ""]
    if included:
        views = {item.id: item for item in plan.digest.findings}
        for index, finding in enumerate(included, 1):
            lines += _comment_finding_lines(plan, finding, index, views.get(finding.finding_key), full=full)
    else:
        lines += ["本次没有通过发布准入的已核验问题。", ""]
    if omitted:
        lines += ["**本次未包含的事项（人工标记已处理，不代表机器重新核验通过）：**", *[
            f"- [{item.severity}] {markdown_text(item.title)}；{_finding_location_text(item)}" for item in omitted], ""]
    if retained:
        lines += [f"{len(retained)} 项已标记处理的阻断问题仍保留正文，需在新快照重新核验。", ""]
    withheld = max(0, plan.outcome.confirmed_count - plan.outcome.publishable_count)
    if withheld:
        lines += [f"另有 {withheld} 项机器确认记录未通过发布准入，详见完整报告；当前评论未将其描述为已核验问题。", ""]
    risks = _comment_risks(plan)
    if risks:
        lines += ["## 未决风险", ""] + risks + [""]
    steps = [line[2:] if line.startswith("- ") else line for line in _next_steps(plan)[3:] if line.strip()]
    lines += ["## 建议", ""] + steps
    if full:
        lines += ["", "## 审查范围", "",
            f"约定目标已检查 {plan.outcome.examined_goals} / {plan.outcome.total_goals}；"
            f"未决候选 {plan.outcome.unresolved_count} 项；未完成调查 {plan.outcome.incomplete_investigations} 项。"]
        lines += _goal_lines(plan.digest.examined_goals, "已检查目标", limit=None)
        lines += _goal_lines(plan.digest.incomplete_goals, "未完成目标", limit=None)
        lines += [f"- 排除文件：{markdown_text(item.path)}；{markdown_text(item.reason)}" for item in plan.digest.excluded_files]
    lines += ["", "---", "",
        f"本结论基于 Commit `{markdown_text(plan.snapshot.head_sha or '未记录', 64)}` 的只读静态审查；"
        "未执行目标代码、测试或构建，不替代 CI 与人工审批。"]
    return "\n".join(lines)


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


def render_technical_audit(plan, execution_record=None):
    import json

    lines = ["# RepoGuardian 技术审计记录", "", "格式化的执行与证据索引，不代表全部原始事件或模型完整输入输出。", "",
        "## 快照与结论身份", "", f"- 审查记录：{markdown_text(plan.review_id)}",
        f"- Head：{markdown_text(plan.snapshot.head_sha or '未知')}",
        f"- Effective Base：{markdown_text(plan.snapshot.effective_base_sha or '未知')}",
        f"- 机器结论：{OUTCOME_LABELS[plan.outcome.kind]}", "", merge_advice(plan), "", "## 验证记录", "", *_validation_lines(plan)]
    for title, data in (("目标符合性与证据引用", [item.model_dump(mode="json") for item in plan.digest.intent_assessments]),
                        ("作者声明输入完整性", {"status": plan.digest.author_context_status,
                            "limitation": plan.digest.author_context_limitation}),
                        ("问题与人工处理身份", [item.model_dump(mode="json") for item in plan.digest.findings]),
                        ("发布准入扣留记录", [item.model_dump(mode="json") for item in plan.withheld_findings]),
                        ("运行、计量与证据追踪", execution_record or {})):
        text = html.escape(json.dumps(data, ensure_ascii=False, indent=2), quote=False)
        fence = "`" * max(3, max((len(value) + 1 for value in re.findall(r"`+", text)), default=3))
        lines += ["", "## " + title, "", fence + "json", text, fence]
    return "\n".join(lines)


def render_summary(plan, *, discussion_urls: dict[str, str] | None = None) -> str:
    outcome = plan.outcome
    digest = plan.digest
    label, advice = merge_heading(plan)
    lines = ["## RepoGuardian · PR Review", "", f"**{label}**", "", advice, "",
             f"- 已确认问题：{outcome.confirmed_count}",
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
            discussion = (discussion_urls or {}).get(finding.finding_key) if finding.placement == "inline" else None
            lines.append(f"{index}. [{finding.severity.title()}] {finding.title} — {location}"
                         + ("（Summary）" if finding.placement == "summary" else "")
                         + (f" · [查看 Inline 讨论]({discussion})" if discussion else ""))
        for finding in plan.summary_findings:
            lines += ["", render_finding(finding)]
    withheld_count = outcome.confirmed_count - outcome.publishable_count
    if withheld_count > 0:
        lines += ["", f"{withheld_count} 项已确认记录未进入发布草稿；具体扣留原因保留在内部发布计划中。"]
    if outcome.reason_codes:
        lines += ["", "### 仍需关注", "", *[f"- {REASON_LABELS[reason]}（{reason.value}）" for reason in outcome.reason_codes]]
    lines += _goal_lines(digest.incomplete_goals, "未完成目标", limit=5, include_reason=False)
    if outcome.incomplete_investigations:
        lines += ["", f"仍有 {outcome.incomplete_investigations} 项调查未完成，需补齐证据与核验。"]
    if digest.termination_reason and digest.termination_reason != "plan_exhausted":
        lines += ["", f"运行终止记录：{markdown_text(digest.termination_reason, 160)}。"]
    lines += ["", "### 验证边界", "", *_validation_lines(plan)]
    if outcome.kind != "clean":
        lines += ["", "此结果不应视为 Approve 或质量检查通过。"]
    if digest.author_context_limitation:
        lines += ["", "作者目标输入限制：" + digest.author_context_limitation]
    lines += ["", f"审查 Commit：{markdown_text(plan.snapshot.head_sha or '未记录', 64)}。", "",
              f"审查记录：{markdown_text(plan.review_id, 160)}。"]
    return "\n".join(lines)
