"""仅渲染后端已准入文本；不访问 GitHub、模型或仓库。"""

import html
import re
import unicodedata
from urllib.parse import quote

from app.models.publication import PublicationFinding, PublicationPlan


OUTCOME_LABELS = {"clean": "未发现可行动问题", "findings": "发现已确认问题", "inconclusive": "本次审查未得出完整结论",
                  "failed": "审查失败", "cancelled": "审查已取消"}
OUTCOME_DESCRIPTIONS = {
    "clean": "已完成本次约定范围的静态审查，未发现具有充分代码证据、需要 PR 作者处理的问题。",
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


def markdown_text(value: str, limit: int = 2000) -> str:
    text = " ".join("".join(char for char in value if not unicodedata.category(char).startswith("C") or char in "\n\t").split())
    text = text[:limit] + ("…" if len(text) > limit else "")
    return re.sub(r"([\\`*_{}\[\]()#+.!|~>-])", r"\\\1", html.escape(text, quote=True)).replace("@", "&#64;")


def render_finding(finding: PublicationFinding) -> str:
    """正文已由 Planner 从 verified publication 文本生成，不再回退候选。"""
    return finding.body_markdown


def finding_body(text: dict, severity: str, head_sha: str, *, category: str | None = None,
                 evidence_summary: str | None = None) -> str:
    label = f"{severity.title()} · {markdown_text(category.title(), 100)}" if category else severity.title()
    evidence = f"**代码证据（核验依据）：** {markdown_text(evidence_summary)}\n\n" if evidence_summary else ""
    return (f"**[{label}] {markdown_text(text['claim'], 200)}**\n\n"
            f"**问题与影响：** {markdown_text(text['affected_behavior'])}\n\n"
            + evidence +
            f"**触发场景：** {markdown_text(text['failure_scenario'])}\n\n"
            f"**建议：** {markdown_text(text['recommendation'])}\n\n"
            "---\n"
            + ("RepoGuardian · 独立核验支持此结论\n\n" if evidence_summary else "RepoGuardian\n\n")
            + "静态审查 · 未执行目标代码、测试或构建\n\n"
            f"审查 Commit：{markdown_text(head_sha, 64)}。")


def render_summary(plan: PublicationPlan, *, discussion_urls: dict[str, str] | None = None) -> str:
    outcome = plan.outcome
    digest = plan.digest
    lines = ["## RepoGuardian · PR Review", "", f"**{OUTCOME_LABELS[outcome.kind]}**", "",
             OUTCOME_DESCRIPTIONS[outcome.kind], "", f"- 已确认问题：{outcome.confirmed_count}",
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


def render_user_report(task, plan: PublicationPlan) -> str:
    outcome = plan.outcome
    lines = ["# RepoGuardian 详细审查报告", "", "## 审查结论", "", f"**{OUTCOME_LABELS[outcome.kind]}**", "",
        OUTCOME_DESCRIPTIONS[outcome.kind], "", merge_advice(plan), "", *_validation_lines(plan)]
    lines += _change_understanding(plan)
    lines += ["", "## 发现的问题", ""]
    findings = _ordered_findings(plan)
    if not findings:
        lines.append("当前没有通过发布准入的已核验问题；以下机器记录与未决风险需分别核对。")
    for finding in findings:
        lines += ["", f"### {finding.title}", "", finding_location(plan, finding), "", render_finding(finding)]
        view = next((item for item in plan.digest.findings if item.id == finding.finding_key), None)
        if view:
            lines += _human_lines(view)
    published_keys = {item.finding_key for item in findings}
    for heading, states in (("机器已确认但未生成发布评论", {"confirmed"}),
                            ("需要人工确认的疑点（尚未确认缺陷）", {"candidate", "unresolved", "unknown"}),
                            ("已排除的候选", {"dismissed"})):
        views = [item for item in plan.digest.findings if item.id not in published_keys and item.review_status in states]
        if views:
            lines += ["", f"### {heading}", ""]
        for view in views:
            lines += [f"#### {markdown_text(view.title or '未命名记录')}", "",
                f"- AI 判定：{ {'confirmed': '机器已确认，发布准入未通过', 'candidate': '候选', 'unresolved': '尚未确认', 'dismissed': '已排除', 'unknown': '未知'}[view.review_status]}",
                f"- 严重程度：{markdown_text(view.severity or '未知')}",
                f"- 位置：{markdown_text(view.location.path + ':' + str(view.location.start_line)) + '（' + view.location.side + '）' if view.location else '尚无可靠代码定位'}"]
            for label, value in (("影响", view.impact), ("触发场景", view.message), ("建议", view.recommendation)):
                if value:
                    lines.append(f"- {label}：{markdown_text(value)}")
            lines += _human_lines(view)
            if view.review_status != "confirmed":
                lines.append("以上为候选或排除记录，不能作为已核验的发布评论。")
    lines += ["", "## 尚未确认的风险", ""]
    if outcome.reason_codes:
        lines += [f"- {REASON_LABELS[reason]}" for reason in outcome.reason_codes]
    lines += ["", "### 哪些调查没有得到答案", ""]
    if not plan.digest.investigations_known:
        lines.append("缺少有效任务运行报告，调查状态未知。")
    else:
        pending = [item for item in plan.digest.investigations if item.answer == "unresolved"]
        lines.append(f"已记录调查 {len(plan.digest.investigations)} 项；有明确答案 "
                     f"{len(plan.digest.investigations) - len(pending)} 项；未得到充分答案 {len(pending)} 项。")
        for index, item in enumerate(pending, 1):
            lines += ["", f"### {index}. {markdown_text(item.proposition)}", "",
                "- 调查结论：尚未取得足够证据回答此问题。",
                f"- 未决原因：{markdown_text(item.reason or '尚未记录明确原因，需查看技术审计记录。')}"]
            limitations = item.limitations
            lines += [f"- 缺失证据或限制：{markdown_text(value)}" for value in limitations]
            if not limitations:
                lines.append("- 缺失证据或限制：未记录具体缺口，不能推断调查已经完成。")
            for request in item.requested_ranges:
                lines.append(f"- 原计划取证范围：{markdown_text(request.path or '未知文件', 500)}:"
                             f"{markdown_text(str(request.start_line))}–{markdown_text(str(request.end_line))}"
                             "（请求范围不代表已读取或已核验）")
            lines += ["- 下一步：补读上述缺失实现、调用方或返回值消费者，再重新核对原命题；证据仍不足时保留未决。",
                      ""]
        if plan.digest.unresolved_leads:
            lines += ["", "尚未完成调度的线索：", *[
                f"- {markdown_text(value)}" for value in plan.digest.unresolved_leads]]
    lines += ["", "## 审查范围与限制", "",
        f"- 审查目标完成：{outcome.examined_goals} / {outcome.total_goals}",
        f"- 已确认问题：{outcome.confirmed_count}；未决候选：{outcome.unresolved_count}"]
    lines += _goal_lines(plan.digest.examined_goals, "已检查目标", limit=None)
    lines += _goal_lines(plan.digest.incomplete_goals, "未完成目标", limit=None)
    if not plan.digest.examined_goals and not plan.digest.incomplete_goals:
        lines += ["", "缺少有效目标记录，不能判断检查范围。"]
    if plan.digest.excluded_files:
        lines += ["", "未纳入本轮审查的文件：", *[
            f"- {markdown_text(item.path, 500)}：{markdown_text(item.reason)}" for item in plan.digest.excluded_files]]
    lines += ["", "## 下一步建议", "", merge_advice(plan)]
    if outcome.confirmed_count:
        lines.append("- 逐项核对已确认问题，结合触发场景和代码证据处理；被扣留的记录仍需单独核验。")
    if plan.digest.incomplete_goals:
        lines.append("- 补查上面列出的未完成目标，再判断整轮审查是否完整。")
    if outcome.incomplete_investigations:
        lines.append("- 优先处理上面的未决调查；初始目标检查完成不代表调用链调查已经得到答案。")
    if outcome.unresolved_count:
        lines.append("- 在 Findings 或原始审计报告中核对未决候选，补齐证据后再决定是否作为缺陷处理。")
    if outcome.reason_codes:
        lines += [f"- {REASON_LABELS[reason]}" for reason in outcome.reason_codes]
    elif outcome.kind == "clean":
        lines.append("- 本轮没有发现需要处理的已确认问题；结合项目自身测试、CI 和人工判断决定是否合并。")
    lines += ["", "## 验证边界", "", *_validation_lines(plan), "", "## 附录：技术信息", "",
        "", f"审查 Commit：{markdown_text(plan.snapshot.head_sha or '未记录', 64)}。",
        f"审查记录：{markdown_text(plan.review_id, 160)}。", "",
        "运行过程与证据追踪请查看技术审计记录；此 Markdown 不代表全部原始事件。"]
    if task.run_manifest:
        lines.append(f"模型：{markdown_text(task.run_manifest.provider)} / {markdown_text(task.run_manifest.model)}。")
    if plan.digest.execution_status:
        lines += ["", f"实际运行记录：{markdown_text(plan.digest.execution_status, 100)} / "
                  f"{markdown_text(plan.digest.termination_reason or 'unknown', 160)}。"]
    if plan.withheld_findings:
        lines += ["", "未进入发布正文的记录：", *[
            f"- {markdown_text(item.candidate_id, 160)}：{markdown_text(item.reason_code)}"
            for item in plan.withheld_findings]]
    if outcome.reason_codes:
        lines += ["", "限制原因代码：" + "、".join(reason.value for reason in outcome.reason_codes)]
    return "\n".join(lines)


def _ordered_findings(plan):
    rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    return sorted([*plan.inline_drafts, *plan.summary_findings], key=lambda item: (
        rank[item.severity], item.anchor.file_path, item.anchor.start_line or 0, item.finding_key))


def merge_advice(plan):
    decision = plan.digest.merge_decision
    lines = ["建议：" + decision.advice,
        f"阻断问题 {len(decision.blocking_finding_keys)} 项；非阻断建议 {len(decision.nonblocking_finding_keys)} 项。"
        "策略：机器确认的 High/Critical 需先修复，Medium/Low 逐项评估；这不是自动 Approve。"]
    lines.append("所有合并判断仍需满足项目要求的 CI 与必要审批；本建议不替代分支保护规则。")
    if decision.unresolved_reasons:
        labels = {"confirmed_findings_withheld": "仍有未通过发布准入的机器确认记录",
            "author_context_truncated": "作者声明输入已截断，未输入部分的目标尚未核验",
            "author_context_unknown": "作者声明输入是否完整未知，需确认目标范围",
            "finding_severity_unknown": "问题严重度未知", "author_intent_unresolved": "作者目标仍有未核验路径或矛盾",
            "head_validation_failed": "当前 Head 验证失败", "head_validation_unknown": "已有验证记录无法确定当前 Head 结果"}
        lines.append("仍需解决：" + "；".join(REASON_LABELS.get(reason, labels.get(reason, reason))
            for reason in decision.unresolved_reasons) + "。")
    return "\n\n".join(lines)


def _change_understanding(plan, *, compact=False):
    changes = plan.digest.actual_changes
    lines = ["", "## PR 变更理解", "",
        "**作者声明的目标（来自 PR 描述或标题，未经独立验证）：** " + markdown_text(plan.digest.author_goal or "未记录作者声明"),
        "", "**实际改动（固定 Diff 事实）：**", "",
        *(["- " + markdown_text(item, 1000) for item in (changes[:5] if compact else changes)] or ["未保存变更明细，无法归纳实际修改。"])]
    if compact and len(changes) > 5:
        lines.append(f"另有 {len(changes) - 5} 个文件变更，完整清单见详细报告。")
    if plan.digest.author_context_limitation:
        lines += ["", "**作者目标输入限制：** " + plan.digest.author_context_limitation]
    checked = [goal for goal in plan.digest.examined_goals if goal.reason]
    lines += ["", "**已检查的关键行为（静态检查记录，不能推导所有路径正确）：**", ""]
    if not checked:
        lines.append("未保存具体行为检查理由，不能从目标数量推断已核对的行为。")
    for goal in (checked[:3] if compact else checked):
        location = markdown_text(goal.path or "文件未记录", 500)
        if goal.start_line:
            location += f":{goal.start_line}" + (f"–{goal.end_line}" if goal.end_line else "")
        lines.append(f"- {location}（{goal.side or '代码侧未知'}）：{markdown_text(goal.reason)}")
    if compact and len(checked) > 3:
        lines.append(f"另有 {len(checked) - 3} 项行为检查记录，详见详细报告。")
    answered = [item for item in plan.digest.investigations if item.answer != "unresolved"]
    if answered:
        lines += ["", "**已回答的关联调查（仅针对记录中的命题）：**", ""]
    for item in (answered[:3] if compact else answered):
        label = "静态证据支持此命题" if item.answer == "supported" else "静态证据反驳此命题"
        lines.append(f"- {markdown_text(item.proposition)}：{label}。依据：{markdown_text(item.reason or '未记录具体理由')}。")
        lines += [f"  - 限制：{markdown_text(value)}" for value in item.limitations]
    if compact and len(answered) > 3:
        lines.append(f"另有 {len(answered) - 3} 项已回答调查，详见详细报告。")
    lines += ["", "**目标符合性：** " + markdown_text(plan.digest.intent_alignment)]
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
        return ["- 人工处理：尚未记录。"]
    label = {"open": "重新打开", "confirmed": "人工确认", "ignored": "已忽略", "resolved": "已处理"}[view.disposition.status]
    return [f"- 人工处理：{label}；原因：{markdown_text(view.disposition.reason)}；"
        f"时间：{markdown_text(view.updated_at.isoformat() if view.updated_at else '未记录')}。机器结论与发布资格保持独立。"]


def finding_location(plan, finding):
    anchor = finding.anchor
    label = markdown_text(anchor.file_path, 1000)
    if anchor.start_line:
        label += f":{anchor.start_line}" + (f"–{anchor.end_line}" if anchor.end_line and anchor.end_line != anchor.start_line else "")
    label += f"（{anchor.side or '代码侧未知'}）"
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
    included, omitted, retained = comment_selection(plan, exclude_resolved=exclude_resolved)
    lines = ["## RepoGuardian · PR 审查评论", "", f"**审查结论：{OUTCOME_LABELS[plan.outcome.kind]}**", "", merge_advice(plan)]
    lines += _change_understanding(plan, compact=detail == "compact")
    lines += ["", "### 发现的问题", ""]
    lines += [f"本次选择：包含 {len(included)} 项问题正文，省略 {len(omitted)} 项已处理的非阻断事项。机器结论与合并建议按全部审查事实保留。", ""]
    for finding in included:
        lines += [finding_location(plan, finding), "", finding.body_markdown, ""]
        view = next((item for item in plan.digest.findings if item.id == finding.finding_key), None)
        if view and view.disposition:
            lines += _human_lines(view)
    if not included:
        lines.append("当前没有通过发布准入的已核验问题正文。")
    if omitted:
        lines += ["", "明确省略的事项（人工标记已处理，不代表机器重新核验通过）：", *[
            f"- [{item.severity}] {item.title}；{finding_location(plan, item)}；原因：本次选择排除已处理的非阻断事项。" for item in omitted]]
    if retained:
        lines.append(f"有 {len(retained)} 项已标记处理的阻断问题仍保留完整正文，需在新快照重新核验。")
    withheld = max(0, plan.outcome.confirmed_count - plan.outcome.publishable_count)
    if withheld:
        lines.append(f"另有 {withheld} 项机器确认记录未通过发布准入，请在详细报告中核对；当前评论未将其描述为已核验问题。")
    lines += ["", "### 其他审查结果与限制", "",
        f"约定目标已检查 {plan.outcome.examined_goals} / {plan.outcome.total_goals}；"
        f"未决候选 {plan.outcome.unresolved_count} 项；未完成调查 {plan.outcome.incomplete_investigations} 项。",
        *[f"- {REASON_LABELS[reason]}" for reason in plan.outcome.reason_codes], "", *_validation_lines(plan), "",
        f"审查 Commit：{markdown_text(plan.snapshot.head_sha or '未记录', 64)}。"]
    if detail == "full":
        lines += _goal_lines(plan.digest.examined_goals, "已检查目标", limit=None)
        lines += _goal_lines(plan.digest.incomplete_goals, "未完成目标", limit=None)
        lines += [f"- 排除文件：{markdown_text(item.path)}；{markdown_text(item.reason)}" for item in plan.digest.excluded_files]
    return "\n".join(lines)


def render_finding_comment(plan, finding):
    lines = [finding_location(plan, finding), "", finding.body_markdown]
    view = next((item for item in plan.digest.findings if item.id == finding.finding_key), None)
    if view:
        lines += ["", *_human_lines(view)]
    other_count = len(_ordered_findings(plan)) - 1
    lines += ["", f"本评论仅针对当前选中的一个问题；另有 {other_count} 项已准入问题未包含在本条评论中。"
        "完整审查结论、被扣留记录与未决风险请查看完整 PR 评论和详细报告。"]
    return "\n".join(lines)


def render_technical_audit(plan):
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
                        ("运行、计量与证据追踪", plan.digest.execution_record)):
        text = html.escape(json.dumps(data, ensure_ascii=False, indent=2), quote=False)
        fence = "`" * max(3, max((len(value) + 1 for value in re.findall(r"`+", text)), default=3))
        lines += ["", "## " + title, "", fence + "json", text, fence]
    return "\n".join(lines)
