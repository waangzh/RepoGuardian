"""仅渲染后端已准入文本；不访问 GitHub、模型或仓库。"""

import html
import re
import unicodedata
from urllib.parse import quote

from app.models.publication import HistoricalFindingView, PublicationFinding, PublicationPlan
from app.services.pr_discussion_matching import exact_duplicate_matches


OUTCOME_LABELS = {"clean": "本次检查未发现需在合并前处理的问题", "findings": "发现代码证据已确认的问题",
                  "inconclusive": "未得出完整结论", "failed": "审查中断，尚无完整结论",
                  "cancelled": "审查已停止，已保留部分结果"}
OUTCOME_DESCRIPTIONS = {
    "clean": "已完成本次约定范围的静态审查，在已检查范围内未发现具有充分代码证据、需要 PR 作者处理的问题。未发现问题不等于证明代码正确。",
    "findings": "请逐项核对下列代码证据已确认的问题及建议；仍需关注的限制见下方说明。",
    "inconclusive": "本轮变更范围内未新增代码证据已确认的问题，但审查仍存在缺口或可靠性限制，不能据此认定本次变更没有问题。",
    "failed": "审查因执行错误中断，请处理运行故障并重新审查；已有记录不能视为完整结论。",
    "cancelled": "审查已由用户停止，请补查剩余范围后再判断本次变更。",
}
REASON_LABELS = {
    "task_failed": "任务或运行时执行失败", "task_cancelled": "任务或运行时已取消",
    "termination_incomplete": "任务未可靠完成全部计划", "scope_incomplete": "部分目标未完成检查",
    "budget_exhausted": "审查预算耗尽，请核对是否仍有未完成目标",
    "deadline_exceeded": "审查达到时间上限，请核对是否仍有未完成目标或调查",
    "scope_empty": "没有实际审查目标", "verification_unresolved": "仍有未核实的候选问题",
    "finding_status_unknown": "存在未知的问题状态", "investigation_incomplete": "仍有未完成或证据不足的调查",
    "unscheduled_investigation": "仍有未处理的调查线索", "usage_integrity_unknown": "模型请求计量不完整或不一致",
    "snapshot_missing": "审查快照缺失或身份无效", "runtime_report_missing": "缺少任务运行报告",
    "runtime_report_invalid": "任务运行报告格式无效", "unsupported_legacy_projection": "历史引擎不支持此发布协议，请查看原始审计报告",
    "historical_discussion_unresolved": "历史讨论复核确认原问题在当前代码中仍存在，需核对",
}
# 对外只保留三种合并建议；review_suggestions 仅为旧冻结计划兼容。
MERGE_LABELS = {
    "fix_blockers": "发现高优先级问题，建议修复后复审",
    "resolve_unknowns": "存在未确认事项，需进一步核实",
    "conditional_merge": "本次检查未发现需在合并前处理的问题",
    "conditional_merge_with_suggestions": "有改进建议，暂无高优先级问题",
    "unavailable": "暂不能给出合并建议",
}
MERGE_SYMBOLS = {
    "fix_blockers": "⚠️",
    "resolve_unknowns": "❓",
    "conditional_merge": "✅",
    "unavailable": "❓",
}
INCOMPLETE_LABELS = {
    "scope_incomplete": "还有约定检查未完成",
    "evidence_gaps": MERGE_LABELS["resolve_unknowns"],
    "runtime_limits": "审查已结束，但结果可靠性仍需核对",
}
_EVIDENCE_REASONS = {
    "verification_unresolved", "finding_status_unknown", "investigation_incomplete",
    "unscheduled_investigation", "confirmed_findings_withheld", "finding_severity_unknown",
    "author_context_truncated", "author_context_unknown", "historical_discussion_unresolved",
}
_RUNTIME_REASONS = {
    "budget_exhausted", "deadline_exceeded", "usage_integrity_unknown", "termination_incomplete",
    "snapshot_missing", "runtime_report_missing", "runtime_report_invalid", "unsupported_legacy_projection",
    "head_validation_failed", "head_validation_unknown",
}


def review_limitation_kind(outcome, reasons=()):
    """仅按保存事实区分范围、代码证据与运行记录限制，不改变 Outcome。"""
    reasons = set(outcome.reason_codes) | set(reasons)
    if reasons & {"scope_incomplete", "scope_empty"} or (
            outcome.total_goals > 0 and outcome.examined_goals < outcome.total_goals):
        return "scope_incomplete"
    if outcome.unresolved_count or outcome.incomplete_investigations or reasons & _EVIDENCE_REASONS:
        return "evidence_gaps"
    if reasons & _RUNTIME_REASONS or outcome.runtime_reliability != "reliable":
        return "runtime_limits"
    return None


def review_limitation_label(outcome, reasons=()):
    reasons = set(outcome.reason_codes) | set(reasons)
    kind = review_limitation_kind(outcome, reasons)
    if kind == "scope_incomplete":
        if not outcome.total_goals:
            return "检查范围尚不明确"
        remaining = outcome.total_goals - outcome.examined_goals
        return f"还有 {remaining} 项约定检查未完成" if remaining > 0 else "尚无法确认检查范围是否完整"
    if kind == "evidence_gaps":
        if outcome.unresolved_count:
            return f"有 {outcome.unresolved_count} 项疑点需要进一步确认"
        if outcome.incomplete_investigations:
            return f"有 {outcome.incomplete_investigations} 项关联调查尚未完成"
        if reasons & {"author_context_truncated", "author_context_unknown"}:
            return "作者声明范围尚待核对"
        if "confirmed_findings_withheld" in reasons:
            return "部分代码证据已确认的记录未通过交付准入，需核对证据"
        return "存在未确认事项，需进一步核实"
    return INCOMPLETE_LABELS.get(kind, "审查结论暂不完整")


def outcome_description(plan) -> str:
    """区分本轮 Diff 与历史复核：已核验的既有风险不能被写成“没有可确认的问题”。"""
    outcome = plan.outcome
    if outcome.kind != "failed" and outcome.kind != "cancelled" \
            and outcome.historical_discussion.affects_risk:
        current = (f"本轮变更范围内发现 {outcome.confirmed_count} 项代码证据已确认的问题" if outcome.confirmed_count
                   else "本轮变更范围内未新增代码证据已确认的问题")
        return (current + "；历史讨论复核确认的既有问题在当前代码中仍然存在，"
                "属于已核验风险，不能据此认定本次变更没有问题。")
    if outcome.kind == "inconclusive" and set(outcome.reason_codes) == {"usage_integrity_unknown"} \
            and outcome.review_completeness == "complete":
        return "已检查范围内未发现已确认问题；运行计量记录不完整，未知用量不能当作完整统计。"
    return OUTCOME_DESCRIPTIONS[outcome.kind]


def historical_risk_line(plan) -> str:
    """历史复核的已核验风险与未完成调查分别计数，避免把未知写成已确认。"""
    outcome = plan.outcome
    history = outcome.historical_discussion
    if not history.affects_risk:
        return ""
    verified = len(history.findings) or len(history.verified_defects)
    pending = _history_entries(plan)[1]
    if not verified:
        return ""
    line = f"历史讨论复核确认 {verified} 项既有问题在当前代码中仍存在；不计入本轮 Diff 新增问题，但会影响合并判断。"
    if pending:
        line += f" 另有 {len(pending)} 项历史事项尚未完成核验，不能据此判断已修复。"
    return line


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


def finding_markdown_title(finding: PublicationFinding) -> str:
    """仅在输出边界转义；旧冻结计划的 title 已转义，不再重复处理。"""
    return markdown_text(finding.raw_title, 200) if finding.raw_title is not None else finding.title


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
        "当前 Head 未执行受控动态验证；Base 参考、独立补丁和历史记录仅适用于各自对象。未发现问题不等于证明代码正确。"
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


def _location_href(plan, finding) -> str | None:
    anchor = finding.anchor
    # 仅从严格解析的 GitHub PR 身份生成链接，不接受仓库提供的 URL。
    from app.tools.github_tool import parse_pr_url
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
    # 优先使用已保存的标签：失败/停止等情景的标签由 Planner 按事实生成，
    # 静态映射只作为旧冻结计划的回退。
    decision = plan.digest.merge_decision
    return decision.label or MERGE_LABELS.get(decision.status, "暂不能给出合并建议"), decision.advice


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


def _independent_count(decision, current: int, historical: int, kind: str) -> int:
    """优先使用 Planner 保存的去重数量；旧冻结计划回退为来源相加。"""
    saved = (decision.blocking_independent_count if kind == "blocking"
             else decision.nonblocking_independent_count)
    return saved if saved is not None else current + historical


def merge_advice(plan):
    label, advice = merge_heading(plan)
    decision = plan.digest.merge_decision
    lines = [f"**{label}**", "", advice]
    if decision.status != "unavailable" or decision.blocking_finding_keys or decision.nonblocking_finding_keys:
        blocking_current = len(decision.blocking_finding_keys)
        blocking_history = len(decision.blocking_historical_keys)
        suggestions_current = len(decision.nonblocking_finding_keys)
        suggestions_history = len(decision.nonblocking_historical_keys)
        # 审计视图同时给出独立缺陷数与两份证据来源，避免相加后的数字被误读。
        lines.append(
            f"独立缺陷：高优先级问题 {_independent_count(decision, blocking_current, blocking_history, 'blocking')} 项，"
            f"非高优先级建议 {_independent_count(decision, suggestions_current, suggestions_history, 'nonblocking')} 项。"
            f"证据来源：本轮记录高优先级 {blocking_current} 项、建议 {suggestions_current} 项；"
            f"历史复核高优先级 {blocking_history} 项、建议 {suggestions_history} 项。"
            "严重度映射不替代对部署条件和业务影响的判断，仍需满足项目要求的 CI 与必要审批。"
            "上述数量与建议由 RepoGuardian 审查策略生成，不代表 GitHub Mergeability 或分支保护结论。")
    if decision.limits:
        lines += ["", "仍需解决：", *[f"- {value}" for value in decision.limits]]
    return "\n\n".join(lines)


def _author_goal(plan):
    understanding = plan.digest.change_understanding
    text = (understanding.author_goal if understanding else plan.digest.author_goal) or ""
    # 兼容旧计划生成的包装，不修改冻结数据，也不删改作者正文中的验证描述。
    prefix = "作者声明（未经独立验证）："
    if text.startswith(prefix):
        text = text.removeprefix(prefix).removesuffix("（作者声明的输入范围有限制，见下方说明。）")
    return text.strip()


def _author_source_lines(plan):
    lines = []
    if _author_goal(plan) not in {"", "未记录明确的作者目标。"}:
        lines.append("PR 目标来自作者提供的 PR 描述或标题，以本轮保存的内容为准；仅作审查背景，不代表目标已实现。")
    if plan.digest.author_context_limitation:
        lines.append("作者目标输入限制：" + markdown_text(plan.digest.author_context_limitation))
    return lines


def _change_understanding(plan, *, full=False):
    understanding = plan.digest.change_understanding
    author_goal = _author_goal(plan) or "未记录明确的作者目标。"
    actual = understanding.actual_changes if understanding else "未保存可归纳的实际变更。"
    alignment = understanding.alignment if understanding else plan.digest.intent_alignment
    lines = ["", "## PR 变更理解", "",
        "**PR 目标：** " + markdown_text(author_goal, 1200 if full else 400), "",
        "**代码实际改变了什么：** " + markdown_text(actual, 2000 if full else 600), "",
        "**吻合与未确认：** " + markdown_text(alignment, 1600 if full else 500)]
    if full and plan.digest.actual_changes:
        lines += ["", "**变更文件（固定 Diff 事实）：**", "",
                  *[f"- {markdown_text(item, 1000)}" for item in plan.digest.actual_changes]]
    if full:
        lines += _intent_detail(plan)
    return lines


def _intent_detail(plan):
    lines = ["", "作者意图对照为辅助分析，不要求逐项核验；未核验项不代表目标未实现。"]
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
    lines = ["", "---", "", f"### {finding_markdown_title(finding)}", "", finding_location(plan, finding), "",
             render_finding(finding)]
    lines += _human_lines(view) if view else []
    return lines


def _appendix_lines(plan, task):
    outcome = plan.outcome
    lines = ["", "## 附录：审查范围与限制", "",
        f"- 审查目标完成：{outcome.examined_goals} / {outcome.total_goals}",
        f"- 本轮 Diff 新增代码证据已确认的问题：{outcome.confirmed_count}；未确认候选：{outcome.unresolved_count}",
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
    lines += ["", "### 来源与验证边界", "", *_author_source_lines(plan), *_validation_lines(plan)]
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
             outcome_description(plan), "",
             f"本轮 Diff 新增代码证据已确认的问题 {outcome.confirmed_count} 项（其中 {outcome.publishable_count} 项形成可发布意见）；"
             f"未确认候选 {outcome.unresolved_count} 项。"]
    history = historical_risk_line(plan)
    if history:
        lines += ["", history]
    if plan.digest.merge_decision.limits:
        lines += ["", "需要补充或确认：", *[f"- {value}" for value in plan.digest.merge_decision.limits]]
    lines += _change_understanding(plan, full=True)
    lines += ["", "## 代码证据已确认的问题", ""]
    findings = _ordered_findings(plan)
    views = {item.id: item for item in plan.digest.findings}
    if not findings:
        # 有已核验历史风险时不能只写“没有问题”，否则结论与章节互相矛盾。
        lines.append("本轮 Diff 没有通过发布准入、具备代码证据的新增问题；"
                     + ("已核验的既有风险见下节。" if _history_entries(plan)[0]
                        else "未确认候选和证据缺口见下方“未决风险”。"))
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
    history_risks = _history_entries(plan)[0]
    if history_risks:
        lines += ["", "## 已核验的既有风险", "",
                  "以下问题经历史讨论复核并以当前固定代码核验仍存在；不计入本轮 Diff 新增问题，但会影响合并判断。", "",
                  *_comment_history_risk_lines(plan, history_risks)]
    lines += _risk_section(plan)
    lines += _next_steps(plan)
    lines += discussion_report_lines(plan, task)
    lines += _appendix_lines(plan, task)
    return "\n".join(lines)


def discussion_report_lines(plan, task=None):
    if plan.digest.discussion_review_enabled is False:
        return ["", "## 历史讨论复核", "", "本次未纳入历史讨论；未采集、未提供给模型，也未执行历史问题复核。"]
    stats = plan.digest.discussion_stats
    if stats is None:
        return ["", "## 历史讨论复核", "", "未记录历史讨论，不能推断该 PR 没有讨论。"]
    history = plan.outcome.historical_discussion
    status_labels = {"completed": "复核完成", "partial": "复核未完整完成", "timed_out": "复核超时中断",
                     "unavailable": "复核记录不可用", "not_recorded": "未记录"}
    lines = ["", "## 历史讨论复核", "",
        f"复核状态：{status_labels.get(history.status, '未知')}；"
        f"已读取并保存 {stats['comments']} 条 PR 评论，整理为 {stats['collected']} 项讨论；"
        f"其中 {stats['checked']} 项已有复核结论，{stats['unchecked']} 项尚未核实充分。",
        f"引用口径：已参考 {stats['referenced']} 项相关讨论，包含 {stats['referenced_comments']} 条评论。"
        "复核结论含代码核对结果和背景说明，不代表每项都经过代码验证。",
        "本节独立于当前 Diff 的审查结论与覆盖率；平台已解决和作者修复声明不是代码证据。"
        "历史复核的超时或计量缺失不改变已完成的主审查结论。"]
    usage = history.usage or {}
    if usage.get("sent_calls"):
        lines.append(f"复核调用：{usage.get('sent_calls')} 次；已结算 {usage.get('settled_tokens', 0)} tokens；"
                     f"未知用量 {usage.get('unknown_attempts', 0)} 次（单独记录，不计入主审查计量）。")
    if history.findings:
        priorities = {"fix_before_merge": "建议合并前修复", "assess_improvement": "评估改进", "assess_impact": "需补核影响"}
        lines.append("经独立代码核验确认仍存在的问题（按当前影响分级）：")
        for item in history.findings:
            lines.append(f"- [{item.severity}] {markdown_text(item.discussion_id)}：{priorities[item.priority]}；"
                         + markdown_text(item.affected_behavior or item.reason))
            if item.recommendation:
                lines.append("  建议：" + markdown_text(item.recommendation))
            lines.append("  代码证据：" + "、".join(markdown_text(e) for e in item.evidence_ids))
            if item.discussion_url:
                lines.append(f"  [查看已有讨论]({item.discussion_url})")
    elif history.verified_defects:
        lines.append("经独立代码核验确认仍存在的问题：")
        for item in history.verified_defects:
            location = markdown_text(item.get("path") or "未记录文件", 500)
            lines.append(f"- {markdown_text(str(item.get('discussion_id')), 200)}（{location}）："
                         + markdown_text(item.get("reason") or "未记录原因"))
    labels = {"still_present": "问题仍存在", "not_present": "当前快照中该主张不成立", "fixed": "已核验修复",
              "insufficient_evidence": "证据不足", "not_checked": "未核验", "background": "背景说明"}
    for item in plan.digest.discussion_rechecks:
        lines += ["", f"- {markdown_text(item['discussion_id'])}：{labels.get(item['status'], '未知')}。",
                  "  " + markdown_text(item["reason"])]
        if item.get("evidence_ids"):
            lines.append("  代码证据：" + "、".join(markdown_text(e) for e in item["evidence_ids"]))
        url = ((getattr(task, "pr_discussion", None) or {}).get("links") or {}).get(item["discussion_id"])
        if url:
            lines.append(f"  [查看已有讨论]({url})")
    lines += [f"- 限制：{markdown_text(value)}" for value in stats["limitations"]]
    return lines


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
        lines.append(f"- 另有 {len(pending) - 5} 项未决调查，完整记录见技术审计记录。")
    answered = [item for item in plan.digest.investigations if item.answer != "unresolved"]
    if answered:
        lines += ["", "### 关键调查摘要", ""]
        for item in answered[:5]:
            label = "静态证据支持此命题" if item.answer == "supported" else "静态证据反驳此命题"
            lines.append(f"- {markdown_text(item.proposition)}：{label}"
                         + (f"；{markdown_text(item.reason)}" if item.reason else "")
                         + "".join(f"；限制：{markdown_text(value)}" for value in item.limitations))
        if len(answered) > 5:
            lines.append(f"- 另有 {len(answered) - 5} 项已回答调查，完整记录见技术审计记录。")
    return lines


def _next_steps(plan):
    outcome = plan.outcome
    lines = ["", "## 下一步建议", ""]
    steps = []
    if plan.digest.merge_decision.blocking_finding_keys or plan.digest.merge_decision.blocking_historical_keys:
        steps.append("- 修复上方高优先级问题并在新快照重新核验；人工标记已处理不能代替机器复核。")
    if plan.digest.merge_decision.nonblocking_finding_keys or plan.digest.merge_decision.nonblocking_historical_keys:
        steps.append("- 逐项评估非高优先级建议的影响与处理方式。")
    if plan.digest.incomplete_goals:
        steps.append("- 补查未完成目标，再判断整轮审查是否完整。")
    if outcome.incomplete_investigations:
        steps.append("- 处理未决调查；初始目标检查完成不代表调用链调查已经得到答案。")
    if outcome.unresolved_count:
        steps.append("- 在问题与证据页核对未决候选，补齐证据后再决定是否作为缺陷处理。")
    if not steps:
        advice = plan.digest.merge_decision.advice if plan.digest.merge_decision.status == "resolve_unknowns" else ""
        steps.append("- " + (advice or "结合项目自身的测试、CI 和人工判断决定是否合并；本轮结论不替代审批。"))
    lines += steps
    return lines


def _comment_risks(plan, *, include_history=True):
    """只列影响合并判断的限制；未逐项核验的作者声明留在详细报告。

    已核验的历史风险在评论中有独立章节，通用限制行不再重复同一条结论；
    尚未完成的历史调查仍由待确认章节逐项呈现。
    """
    risks = [value for value in plan.digest.merge_decision.limits
             if include_history or value != REASON_LABELS["historical_discussion_unresolved"]]
    if "usage_integrity_unknown" in plan.outcome.reason_codes:
        risks.append("运行计量记录不完整：模型请求计量不完整或不一致；未知用量不能当作完整统计。"
                     "运行或计量异常本身不代表发现代码缺陷。")
    for item in plan.digest.intent_assessments:
        if item.status == "contradicted":
            locations = []
            for observation in item.observations:
                if observation.get("alignment") != "contradicted":
                    continue
                path = observation.get("path") or "未知文件"
                line = observation.get("start_line")
                locations.append(f"{path}:{line}" if line else path)
            suffix = "，位置：" + "、".join(dict.fromkeys(locations)) if locations else ""
            risks.append(f"作者声明“{_sentence(item.author_target, 120)}”与代码存在静态证据矛盾{suffix}。")
    pending = [item for item in plan.digest.investigations if item.answer == "unresolved"]
    if pending:
        risks.append(f"{len(pending)} 项调查未得到充分答案，相关调用链结论仍需补充证据。")
    if plan.digest.validation_state == "failed":
        risks.append(_validation_sentence(plan))
    return [f"- {value}" for value in dict.fromkeys(risks)]


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


def _history_entries(plan):
    """把历史复核分成“已核验的既有风险”和“尚未完成的调查”。

    只有取得独立代码证据且判定仍存在的记录才是已核验风险；平台 resolved、
    人工注记和缺少证据的模型判断都留在待确认一侧。已被当前问题按严格重复
    关联覆盖的历史记录不重复展示，但仍保留在合并决策的来源计数中。
    """
    history = plan.outcome.historical_discussion
    represented = {match.get("discussion_id") for finding in _ordered_findings(plan)
                   for match in exact_duplicate_matches(finding.discussion_matches)}
    assessed = {item.discussion_id: item for item in history.findings}
    # 旧冻结计划可能没有 findings 字段；有证据编号的 verified_defects 仍是已核验风险。
    legacy = {str(item.get("discussion_id")): item for item in history.verified_defects
              if item.get("discussion_id") and item.get("evidence_ids")}
    risks, pending = [], []
    for item in plan.digest.discussion_rechecks:
        status = item.get("status")
        identity = item.get("discussion_id") or "未记录"
        if status in {"fixed", "not_present", "background"} or identity in represented:
            continue
        finding = assessed.get(identity)
        if finding is None and identity in legacy:
            finding = HistoricalFindingView(discussion_id=identity, path=legacy[identity].get("path"),
                severity="unknown", priority="assess_impact",
                reason=legacy[identity].get("reason") or "未记录原因",
                evidence_ids=list(legacy[identity]["evidence_ids"]),
                discussion_url=_history_url(plan, identity))
        if finding is not None:
            risks.append(finding)
        elif status == "still_present":
            pending.append((identity, "复核仍存在，但缺少可引用的代码证据，尚不能作为已核验风险", item))
        else:
            pending.append((identity, "待核实，不能判断已修复", item))
    return risks, pending


def _history_url(plan, identity):
    return plan.digest.discussion_links.get(identity)


def _history_priority(finding) -> str:
    return {"fix_before_merge": "建议合并前修复", "assess_improvement": "评估改进",
            "assess_impact": "严重度及影响尚待核对"}[finding.priority]


def _comment_history_risk_lines(plan, risks):
    """已核验的既有风险按正式问题格式呈现，并明确不计入本轮 Diff 新增问题。"""
    lines = []
    for finding in risks:
        title = finding.affected_behavior or finding.reason
        lines += [f"**[{finding.severity.title()} · 历史复核] {markdown_text(title, 200)}**", "",
                  f"**代码位置：** {markdown_text(finding.path, 500) if finding.path else '未记录文件'}", ""]
        lines += [f"**复核结论：** {_history_priority(finding)}；{markdown_text(finding.reason)}", ""]
        if finding.recommendation:
            lines += [f"**建议：** {markdown_text(finding.recommendation)}", ""]
        if finding.evidence_ids:
            lines += [f"**代码证据：** {'、'.join(markdown_text(item) for item in finding.evidence_ids)}", ""]
        url = finding.discussion_url or _history_url(plan, finding.discussion_id)
        if url:
            lines += [f"[历史讨论]({url})", ""]
    return lines


def _comment_history_risk_section(plan, risks):
    """已核验的既有风险使用正式风险章节，而不是与未完成调查混在待确认事项中。"""
    if not risks:
        return []
    return ["", "## 已核验的既有风险", "",
            "以下问题经历史讨论复核并以当前固定代码核验仍存在；不计入本轮 Diff 新增问题，但会影响合并判断。", "",
            *_comment_history_risk_lines(plan, risks)]


def _comment_history_pending_lines(plan, pending):
    """尚未完成或缺少证据的历史调查仍进入待确认章节，并说明不能据此判断修复。"""
    lines = []
    for identity, label, item in pending:
        url = _history_url(plan, identity)
        source = f"[历史讨论]({url})" if url else f"历史讨论 {markdown_text(identity)}"
        lines.append(f"- {source}：{label}；{markdown_text(item.get('reason') or '缺少充分代码证据', 240)}。")
    return lines


def _comment_history_lines(plan):
    """交付摘要使用的一行式历史状态；已核验风险与待确认调查分别表述。"""
    risks, pending = _history_entries(plan)
    lines = []
    for finding in risks:
        lines.append(f"- 历史复核仍存在：[{finding.severity}] {markdown_text(finding.affected_behavior or finding.reason, 240)}；"
                     f"{_history_priority(finding)}；{markdown_text(finding.path or '未记录文件', 500)}。"
                     "不计入本轮 Diff 新增问题。")
        if finding.recommendation:
            lines.append("  建议：" + markdown_text(finding.recommendation))
        if finding.evidence_ids:
            lines.append("  代码证据：" + "、".join(markdown_text(item) for item in finding.evidence_ids))
    lines += _comment_history_pending_lines(plan, pending)
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
        lines += [f"**PR 目标：** {markdown_text(author, 400)}（作者声明，不代表目标已实现）", ""]
    lines += [f"**代码观察：** {markdown_text(actual or '未保存可归纳的实际变更。', 800)}"]
    return lines


def _comment_other_results(plan, *, omitted, retained, withheld, full, title=None, extra_lines=()):
    """只展开实际缺口；通用验证边界统一放在文末。

    章节标题按内容决定：确实存在未确认事项时使用“仍需确认的事项”；
    只有完整版的审查范围、来源等补充信息时使用“审查范围与补充信息”，
    不让完整明细本身制造一个并不存在的风险章节。简洁版与完整版共享同一
    判断结果，完整版只追加明细，不改变章节是否存在。
    """
    # 已核验风险有独立章节，避免通用限制行与它重复表达同一事实。
    risks = _comment_risks(plan, include_history=not _history_entries(plan)[0])
    pending = _history_entries(plan)[1]
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
    entries += _comment_history_pending_lines(plan, pending)
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


def _validation_sentence(plan) -> str:
    state = plan.digest.validation_state
    return {
        "not_executed": ("当前 Head 未执行受控动态验证；Base 参考与独立补丁结果仅适用于各自对象。"
            if plan.digest.validations else "未执行目标仓库代码、测试或构建。"),
        "passed": "已有与当前 Head 绑定的受控验证通过记录；不代替完整 CI 或审批。",
        "failed": "已有与当前 Head 绑定的验证失败记录，需核对失败原因。",
        "independent_results": "已有独立补丁验证结果；补丁通过不代表当前 PR Head 已通过测试。",
        "unknown": "动态验证状态未知；不能据此宣称目标仓库测试或构建已通过。",
    }[state]


def comment_selection(plan, *, exclude_resolved=False, include_discussed=False):
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
        steps.append("结合 CI、业务预期和人工评审决定是否合并。已检查范围内未发现需在合并前处理的问题，不等于代码无缺陷。")
    if plan.digest.merge_decision.nonblocking_finding_keys and decision.status == "fix_blockers":
        steps.append("非高优先级建议可在同一轮修改中一并评估。")
    if plan.digest.incomplete_goals:
        steps.append("未完成的约定目标需要补查后才能判断整轮审查是否完整。")
    return [" ".join(steps)]


def render_review_comment(plan, *, detail="compact", exclude_resolved=False, include_discussed=False):
    """固定章节语义，只按风险调整顺序；没有内容的章节不输出。

    章节含义在所有状态下一致：结论、本次变更理解、需要处理的问题、其他建议、
    仍需确认的事项、下一步、快照与验证边界。这样不同 PR 复制出的评论遵循同一套阅读顺序。
    """
    included, omitted, retained = comment_selection(plan, exclude_resolved=exclude_resolved,
                                                     include_discussed=include_discussed)
    full = detail == "full"
    withheld = max(0, plan.outcome.confirmed_count - plan.outcome.publishable_count)
    lines = ["RepoGuardian · PR Code Review", "", merge_verdict_line(plan), ""]
    if plan.digest.discussion_review_enabled is False:
        lines += ["本次未纳入历史讨论；以下结论不包含历史讨论复核。", ""]
    if plan.digest.discussion_stats:
        stats = plan.digest.discussion_stats
        lines += [f"当前结论针对本轮变更范围；已参考 {stats['referenced']} 项历史讨论，"
                  f"另有 {stats['unchecked']} 项历史事项未核验。", ""]
    history = historical_risk_line(plan)
    if history:
        lines += [history, ""]
    if any(item.default_selection_reason == "historical_duplicate" for item in included):
        lines += ["已有历史讨论的当前问题仍保留风险摘要；已有讨论不代表已修复。", ""]
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
        or plan.outcome.runtime_reliability != "reliable"
        or plan.digest.merge_decision.status == "resolve_unknowns")
    views = {item.id: item for item in plan.digest.findings}

    def finding_section(findings, title):
        # 只有实际条目才输出章节，避免出现“建议”标题后只有行动文案的空章节。
        if not findings:
            return []
        result = ["", f"## {title}", ""]
        for finding in findings:
            discussed = finding.default_selection_reason == "historical_duplicate"
            needs_evidence = (full or (discussed and include_discussed) or finding.severity in {"high", "critical"}
                or finding.finding_key in blockers)
            if discussed and not include_discussed and not full:
                needs_evidence = False
            result += _comment_finding_lines(plan, finding, views.get(finding.finding_key), full=needs_evidence)
            if discussed:
                from app.services.pr_discussion_matching import safe_discussion_url

                urls = [safe for url in finding.discussion_urls
                        if (safe := safe_discussion_url(url, plan.digest.pr_url))]
                links = "、".join(f"[历史讨论 {i}]({url})" for i, url in enumerate(urls, 1))
                result += ["当前快照仍确认此问题。" + (f"已有讨论：{links}。" if links else "未保存可用的历史讨论链接。"), ""]
        return result

    scope_lines = []
    if full:
        scope_lines += ["", "### 审查范围", "",
            f"约定目标已检查 {plan.outcome.examined_goals} / {plan.outcome.total_goals}；"
            f"未决候选 {plan.outcome.unresolved_count} 项；未完成调查 {plan.outcome.incomplete_investigations} 项。"]
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

    # 已核验的历史风险进入正式风险章节；未完成的历史调查留在待确认章节。
    history_section = _comment_history_risk_section(plan, _history_entries(plan)[0])
    if interrupted:
        lines += understanding_section()
        scope = (f"约定目标已检查 {plan.outcome.examined_goals} / {plan.outcome.total_goals} 项"
                 if plan.outcome.total_goals else "约定目标的检查范围未知")
        lines += ["", scope + "；以上仅为已保存的检查观察，不代表完整结论。"]
        lines += history_section
        lines += limitations()
        lines += next_steps_section()
    elif blocking_findings:
        lines += finding_section(blocking_findings, "需要处理的问题")
        lines += history_section
        lines += understanding_section()
        lines += finding_section(suggestions, "其他建议")
        lines += limitations()
        lines += next_steps_section()
    elif suggestions or omitted:
        lines += history_section
        lines += understanding_section()
        lines += finding_section(suggestions, "其他建议")
        lines += limitations()
        lines += next_steps_section()
    elif incomplete:
        lines += history_section
        lines += understanding_section()
        lines += ["", "> 以上变更理解仅基于已保存的局部观察或变更元数据，"
                  "不代表所有目标已完成核验。"]
        lines += limitations() or [
            "", "## 仍需确认的事项", "", "缺口或运行记录尚不明确，请先核对详细报告。"]
        lines += next_steps_section()
    else:
        lines += history_section
        lines += understanding_section()
        lines += limitations()
        lines += next_steps_section()
    validation = _validation_sentence(plan)
    boundary = "" if any(validation in line for line in lines) else validation
    subject = "本记录" if note else "本结论"
    lines += ["", "---", "",
        f"{subject}基于提交 `{markdown_text(plan.snapshot.head_sha or '未记录', 64)}` 的只读审查；"
        + boundary + "不替代 CI 与人工审批。"]
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


def render_technical_audit(plan, execution_record=None):
    import json

    lines = ["# RepoGuardian 技术审计记录", "", "格式化的执行与证据索引，不代表全部原始事件或模型完整输入输出。", "",
        "## 快照与结论身份", "", f"- 审查记录：{markdown_text(plan.review_id)}",
        f"- Head：{markdown_text(plan.snapshot.head_sha or '未知')}",
        f"- Effective Base：{markdown_text(plan.snapshot.effective_base_sha or '未知')}",
        f"- 机器结论：{OUTCOME_LABELS[plan.outcome.kind]}"]
    if plan.outcome.kind == "failed":
        from app.services.task_review_projection import runtime_failure_summary
        record = execution_record or {}
        failure = record.get("failure_summary") or runtime_failure_summary(
            record.get("diagnostics") or [], termination_reason=record.get("termination_reason"))
        lines.append(f"- 失败原因：{markdown_text(failure)}")
    lines += ["", merge_advice(plan), "", "## 验证记录", "", *_validation_lines(plan)]
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
    """GitHub 交付摘要：复用评论措辞，运行与证据明细留在内部报告。"""
    outcome = plan.outcome
    digest = plan.digest
    lines = ["## RepoGuardian · PR Review", "", merge_verdict_line(plan), ""]
    if digest.discussion_stats:
        stats = digest.discussion_stats
        lines += [f"已参考 {stats['referenced']} 项相关讨论；历史复核独立于当前变更结论，"
                  f"{stats['unchecked']} 项尚未核验。", ""]
    history = historical_risk_line(plan)
    if history:
        lines += [history, ""]
    note = partial_review_note(plan)
    if note:
        lines += [note, ""]
    lines += [f"本轮 Diff 新增代码证据已确认的问题：{outcome.confirmed_count}；"
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
    risks += _comment_history_lines(plan)
    if risks:
        lines += ["", "### 仍需关注", "", *dict.fromkeys(risks)]
    if digest.author_context_limitation and not any(digest.author_context_limitation in line for line in risks):
        lines += ["", "作者目标输入限制：" + markdown_text(digest.author_context_limitation)]
    lines += ["", "### 下一步", "", *_comment_next_steps(plan, has_findings=bool(findings))]
    validation = _validation_sentence(plan)
    boundary = "" if any(validation in line for line in lines) else validation
    lines += ["", "---", "", f"审查 Commit：`{markdown_text(plan.snapshot.head_sha or '未记录', 64)}`；"
              + boundary + "本结果不替代 CI 与人工审批。"]
    return "\n".join(lines)
