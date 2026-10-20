"""报告编辑性契约：评论面向作者、详细报告不重复、概览不携带审计数据。"""

from copy import deepcopy

import pytest

from app.models.review_runtime import IntentAssessment
from app.services.publication_planner import PublicationPlanner, summary_projection
from app.services.publication_renderer import render_review_comment, render_user_report
from app.services.review_intent import author_targets
from test_publication_planner import publishable_task
from test_review_outcome import completed_task


def test_author_targets_skip_markdown_scaffolding_and_non_goal_sections():
    context = ("## 背景\n这是无关背景。\n\n## 修改内容\n- 拒绝过期令牌。\n- 补充刷新路径。\n\n"
               "## 测试\n运行 pytest。\n\n## 注意事项\n- 不要修改数据库。\n\n"
               "## 现状与问题\n旧实现会重复查询。\n\n## 其他说明\n后续再补文档。\n\n"
               "```python\nprint('x')\n```\n| 字段 | 说明 |\n| --- | --- |")
    assert author_targets(context) == ["拒绝过期令牌。", "补充刷新路径。"]
    # 普通段落仍按句末标点切分，保留原文。
    assert author_targets("保留返回值。拒绝空值。") == ["保留返回值。", "拒绝空值。"]


def test_change_understanding_uses_bound_observations_and_does_not_claim_fulfillment():
    task = publishable_task()
    goal = task.task_runtime_report["initial_scope_coverage"][0]
    goal.update(target_ref="src/a.py", target_kind="source_range", status="examined", result_id="result-1",
        snapshot_head_sha=task.run_manifest.head_sha, snapshot_base_sha=task.run_manifest.effective_base_sha,
        evidence_ids=["e1"], intent_assessments=[IntentAssessment(author_quote="拒绝空值。",
            actual_behavior="空值分支在读取字段前返回", alignment="supported", evidence_ids=["e1"]).model_dump(mode="json")])
    task.task_runtime_report["pr_intent_context"] = "拒绝空值。"
    task.task_runtime_report["pr_intent_context_truncated"] = False
    plan = PublicationPlanner().build(task)
    understanding = plan.digest.change_understanding
    assert understanding is not None
    assert understanding.author_goal == "拒绝空值。"
    assert plan.digest.author_context_status == "complete"
    assert "空值分支在读取字段前返回" in understanding.actual_changes
    assert "1 项获得局部静态证据支持" in understanding.alignment
    assert "不构成作者全部目标已实现的验收结论" in understanding.alignment
    assert understanding.author_goal in plan.review_comment_markdown


def test_unbound_intent_assessment_never_becomes_change_claim():
    task = publishable_task()
    goal = task.task_runtime_report["initial_scope_coverage"][0]
    goal.update(target_ref="src/a.py", status="examined", result_id="result-1", evidence_ids=["e1"],
        snapshot_head_sha="d" * 40, snapshot_base_sha=task.run_manifest.effective_base_sha,
        intent_assessments=[IntentAssessment(author_quote="拒绝空值。", actual_behavior="未经绑定的行为",
            alignment="supported", evidence_ids=["e1"]).model_dump(mode="json")])
    task.task_runtime_report["pr_intent_context"] = "拒绝空值。"
    plan = PublicationPlanner().build(task)
    # 未绑定的观察不能进入变更理解或评论；详细报告只作为“证据不足”的记录保留。
    assert "未经绑定的行为" not in plan.review_comment_markdown
    assert "未经绑定的行为" not in (plan.digest.change_understanding.actual_changes if plan.digest.change_understanding else "")
    assert "未经绑定的行为" in plan.user_report_markdown
    assert "证据不足" in plan.user_report_markdown


def test_compact_comment_is_publishable_and_does_not_repeat_boilerplate():
    task = publishable_task(claim="回归令牌校验缺失", severity="high")
    task.task_runtime_report["findings"][0]["issue"]["confidence"] = 0.9
    plan = PublicationPlanner().build(task)
    compact = render_review_comment(plan, detail="compact")
    assert compact.count("本结论基于提交") == 1
    assert "独立核验支持此结论" not in compact
    assert "静态审查 · 未执行目标代码" not in compact
    assert compact.index("回归令牌校验缺失") < compact.index("本结论基于提交")
    # 省略没有实际限制的空章节，同时保留问题与快照。
    assert compact.startswith("RepoGuardian · PR Code Review\n\n✅ 结论：发现 1 项改进建议，未发现阻断性问题")
    assert [line for line in compact.splitlines() if line.startswith("## ")] == ["## 变更理解", "## 建议项"]
    assert "非阻断问题" in compact
    # 非阻断问题也必须保留标题、位置和建议，只是省略证据展开。
    assert "**建议：**" in compact and "src/a" in compact


def test_detailed_report_states_conclusion_limits_and_steps_once():
    task = completed_task(scope_complete=False)
    task.task_runtime_report["initial_scope_coverage"].append(
        {"goal_id": "g2", "target_ref": "src/b.py", "status": "not_examined", "reason": "预算耗尽"})
    plan = PublicationPlanner().build(task)
    report = render_user_report(task, plan)
    assert report == plan.user_report_markdown
    # 结论、建议、验证边界各出现一次；范围目标只出现在附录。
    assert report.count("## 审查结论") == 1
    assert report.count("## 下一步建议") == 1
    assert report.count("### 来源与验证边界") == 1
    assert report.index("## 附录：审查范围与限制") < report.index("### 来源与验证边界")
    assert "src/b.py" not in report.split("## 附录")[0]
    assert report.count("仍以审查范围与验证边界为准") <= 1


def test_clean_comment_is_brief_and_keeps_one_validation_boundary():
    task = completed_task()
    task.task_runtime_report["pr_intent_context"] = "修复空值异常。"
    task.task_runtime_report["pr_intent_context_truncated"] = False
    plan = PublicationPlanner().build(task)
    markdown = plan.review_comment_markdown
    for phrase in ("**PR 目标：** 修复空值异常。", "不代表目标已实现", "**代码观察：**",
                   "已检查范围内未发现需处理的问题", "本结论基于提交"):
        assert phrase in markdown
    assert plan.outcome.confirmed_count == 0
    assert markdown.count("未执行目标仓库代码、测试或构建") == 1
    assert markdown.count("不替代 CI 与人工审批") == 1
    assert [line for line in markdown.splitlines() if line.startswith("## ")] == ["## 变更概述", "## 合并前提醒"]
    assert "作者声明（未经独立验证）：" not in markdown
    for state in ("passed", "failed", "independent_results", "unknown"):
        changed = plan.model_copy(deep=True)
        changed.digest.validation_state = state
        result = render_review_comment(changed)
        assert "没有本轮独立运行的测试或构建结果" not in result
        if state == "passed":
            assert "受控验证通过记录" in result
        elif state == "independent_results":
            assert "补丁通过不代表当前 PR Head 已通过测试" in result
        elif state == "unknown":
            assert result.count("动态验证状态未知") == 1
        elif state == "failed":
            assert result.count("已有与当前 Head 绑定的验证失败记录，需核对失败原因。") == 1


def test_author_goal_is_plain_text_and_legacy_wrapping_is_display_only():
    task = publishable_task()
    task.task_runtime_report["pr_intent_context"] = "修复缓存失效时的读取问题。"
    task.task_runtime_report["pr_intent_context_truncated"] = True
    plan = PublicationPlanner().build(task)
    understanding = plan.digest.change_understanding
    assert understanding.author_goal == "修复缓存失效时的读取问题。"
    assert plan.digest.author_context_status == "truncated"
    assert plan.digest.intent_assessments[0].status == "not_checked"
    understanding.author_goal = "作者声明（未经独立验证）：修复缓存失效时的读取问题。（作者声明的输入范围有限制，见下方说明。）"
    before = plan.model_dump(mode="json")
    for markdown in (render_review_comment(plan), render_review_comment(plan, detail="full"), render_user_report(task, plan)):
        assert "**PR 目标：** 修复缓存失效时的读取问题。" in markdown
        assert "作者声明（未经独立验证）：" not in markdown
        assert "输入范围有限制，见下方说明" not in markdown
        if "来源与验证边界" in markdown:
            assert markdown.count("PR 目标来自作者提供的 PR 描述或标题") == 1
            assert markdown.index("来源与验证边界") < markdown.index("作者目标输入限制：")
        assert "送审内容已截断" in markdown
    assert plan.model_dump(mode="json") == before


def test_multiple_findings_share_one_document_validation_boundary():
    task = publishable_task(claim="缓存读取失败")
    second = deepcopy(publishable_task(claim="刷新路径失败", candidate_id="candidate-b").task_runtime_report["findings"][0])
    second["key"] = "key-b"
    task.task_runtime_report["findings"].append(second)
    plan = PublicationPlanner().build(task)
    assert plan.outcome.publishable_count == 2
    for markdown in (plan.user_report_markdown, render_review_comment(plan, detail="full")):
        assert "缓存读取失败" in markdown and "刷新路径失败" in markdown
        assert markdown.count("未执行目标仓库代码") == 1
        assert markdown.count("来源与验证边界") <= 1


@pytest.mark.parametrize("severity", ["high", "critical"])
def test_compact_blockers_keep_evidence_and_trigger_without_changing_admission(severity):
    task = publishable_task(severity=severity, claim="权限绕过导致未授权数据访问")
    task.task_runtime_report["findings"][0]["issue"]["confidence"] = 0.99
    plan = PublicationPlanner().build(task)
    before = plan.model_dump(mode="json")
    finding = plan.inline_drafts[0]
    assert finding.severity == severity
    for detail in ("compact", "full"):
        markdown = render_review_comment(plan, detail=detail, exclude_resolved=True)
        assert "**代码证据：** " + finding.evidence_summary in markdown
        assert "**触发场景：** " + finding.failure_scenario in markdown
        assert "**建议：** " + finding.recommendation in markdown
    assert plan.model_dump(mode="json") == before


@pytest.mark.parametrize("updates,reason", [
    ({"scope_complete": False, "initial_scope_coverage": [
        {"goal_id": "gap", "target_ref": "src/missing.py", "status": "not_examined", "reason": "上下文不足"}]}, "上下文不足"),
    ({"termination_reason": "budget_exhausted"}, "预算耗尽"),
    ({"usage_integrity": "unknown"}, "计量不完整"),
])
def test_incomplete_comments_only_show_gaps_and_next_steps(updates, reason):
    plan = PublicationPlanner().build(completed_task(**updates))
    for detail in ("compact", "full"):
        markdown = render_review_comment(plan, detail=detail)
        assert reason in markdown
        assert [line for line in markdown.splitlines() if line.startswith("## ")] == [
            "## 未检查/未核实事项", "## 下一步"]
        assert "## 变更概述" not in markdown and "## 已确认问题" not in markdown
        assert markdown.count("未执行目标仓库代码、测试或构建") == 1


@pytest.mark.parametrize("severity", ["high", "critical"])
@pytest.mark.parametrize("updates", [
    {"usage_integrity": "unknown"},
    {"scope_complete": False},
    {"termination_reason": "budget_exhausted"},
    {"admitted_investigations": [{"status": "not_executed"}]},
])
def test_blockers_precede_observations_and_limits_even_with_execution_gaps(severity, updates):
    task = publishable_task(severity=severity, claim="权限绕过导致未授权数据访问")
    task.task_runtime_report["findings"][0]["issue"]["confidence"] = 0.99
    task.task_runtime_report.update(updates)
    before = task.model_dump(mode="json")
    plan = PublicationPlanner().build(task)
    assert plan.digest.merge_decision.status == "fix_blockers"
    assert plan.outcome.publishable_count == 1
    for detail in ("compact", "full"):
        markdown = render_review_comment(plan, detail=detail)
        assert [line for line in markdown.splitlines() if line.startswith("## ")] == [
            "## 阻断问题", "## 变更理解", "## 其他局限", "## 建议"]
        assert "**代码证据：**" in markdown and "**触发场景：**" in markdown
        assert markdown.count("权限绕过导致未授权数据访问") == 1
    assert task.model_dump(mode="json") == before


def test_nonblocking_findings_remain_before_runtime_limits():
    task = publishable_task()
    task.task_runtime_report["usage_integrity"] = "unknown"
    plan = PublicationPlanner().build(task)
    assert plan.digest.merge_decision.nonblocking_finding_keys == ["key-a"]
    for detail in ("compact", "full"):
        markdown = render_review_comment(plan, detail=detail)
        assert [line for line in markdown.splitlines() if line.startswith("## ")] == [
            "## 变更理解", "## 建议项", "## 局限"]


def test_mixed_findings_only_put_blockers_before_change_understanding():
    task = publishable_task(severity="high", claim="权限绕过导致未授权数据访问")
    task.task_runtime_report["findings"][0]["issue"]["confidence"] = 0.99
    other = publishable_task(claim="缓存改进建议", candidate_id="candidate-b").task_runtime_report["findings"][0]
    other["key"] = "key-b"
    task.task_runtime_report["findings"].append(other)
    task.task_runtime_report["usage_integrity"] = "unknown"
    plan = PublicationPlanner().build(task)
    assert plan.outcome.publishable_count == 2
    before = plan.model_dump(mode="json")
    for detail in ("compact", "full"):
        markdown = render_review_comment(plan, detail=detail)
        assert markdown.index("权限绕过导致未授权数据访问") < markdown.index("## 变更理解")
        assert markdown.index("缓存改进建议") > markdown.index("## 其他局限")
        assert "缓存改进建议" not in markdown.split("## 建议\n")[0]
        assert markdown.count("缓存改进建议") == 1
        assert markdown.count("权限绕过导致未授权数据访问") == 1
    assert plan.model_dump(mode="json") == before


def test_comment_does_not_silently_drop_risks_after_six_entries():
    plan = PublicationPlanner().build(completed_task())
    plan.digest.merge_decision.limits = [f"需要复核的独立限制 {i}" for i in range(8)]
    markdown = render_review_comment(plan)
    for limit in plan.digest.merge_decision.limits:
        assert limit in markdown


def test_summary_projection_excludes_audit_and_per_finding_payload():
    task = publishable_task()
    plan = PublicationPlanner().build(task)
    summary = summary_projection(plan)
    dumped = summary.model_dump(mode="json")
    assert dumped["schema_version"] == "review-summary-v1"
    assert dumped["review_comment_markdown"] == plan.review_comment_markdown
    digest = dumped["digest"]
    for key in ("execution_record", "findings", "examined_goals", "incomplete_goals", "investigations"):
        assert key not in digest
    assert "摘要" not in dumped and len(dumped["digest"]) < len(plan.digest.model_dump(mode="json"))


def test_unchecked_author_claims_do_not_block_merge_but_contradictions_are_shown():
    task = publishable_task()
    report = task.task_runtime_report
    report["pr_intent_context"] = "拒绝空值。补充测试。"
    report["pr_intent_context_truncated"] = False
    plan = PublicationPlanner().build(task)
    unchecked = next(item for item in plan.digest.intent_assessments if item.author_target == "补充测试。")
    assert unchecked.status == "not_checked"
    assert "author_intent_unresolved" not in plan.digest.merge_decision.unresolved_reasons
    # 对外只有三种合并建议；已确认的 Medium/Low 归入非阻断建议。
    assert plan.digest.merge_decision.status == "conditional_merge"
    assert plan.digest.merge_decision.nonblocking_finding_keys == ["key-a"]
    # 未核验只保留声明边界，不成为阻断；明确矛盾才展示位置。
    assert "”尚未核验。" not in plan.review_comment_markdown
    goal = report["initial_scope_coverage"][0]
    goal.update(target_ref="src/a.py", status="examined", result_id="result-1", evidence_ids=["e1"],
        snapshot_head_sha=task.run_manifest.head_sha, snapshot_base_sha=task.run_manifest.effective_base_sha,
        intent_assessments=[IntentAssessment(author_quote="拒绝空值。", actual_behavior="空值被直接放行",
            alignment="contradicted", evidence_ids=["e1"]).model_dump(mode="json")])
    contradicted = PublicationPlanner().build(task)
    assert "静态证据矛盾" in contradicted.review_comment_markdown
    assert "src/a.py" in contradicted.review_comment_markdown
    assert "author_intent_unresolved" not in contradicted.digest.merge_decision.unresolved_reasons


def test_merge_decision_separates_code_blockers_from_runtime_limits():
    bounded = PublicationPlanner().build(completed_task(termination_reason="budget_exhausted"))
    assert bounded.digest.merge_decision.status == "resolve_unknowns"
    # 只有执行边界限制时，不把结论描述成“代码有问题”。
    assert bounded.digest.merge_decision.label == "审查运行记录存在限制，需核对结果完整性"
    assert "执行、计量或验证记录" in bounded.digest.merge_decision.advice
    assert "审查预算耗尽，请核对是否仍有未完成目标" in bounded.digest.merge_decision.limits
    assert "修复上方阻断问题" not in bounded.user_report_markdown
    clean = PublicationPlanner().build(completed_task())
    assert clean.digest.merge_decision.status == "conditional_merge"
    assert clean.digest.merge_decision.label == "已检查范围内未发现需处理的问题"
    assert clean.digest.merge_decision.limits == []


def test_failed_or_cancelled_copy_is_explicitly_a_partial_record():
    """失败/取消后允许复制，但正文必须自述为部分记录，不能呈现为完成的 PR Review。"""
    failed = publishable_task()
    failed.status = "failed"
    failed.task_runtime_report["execution_status"] = "failed"
    plan = PublicationPlanner().build(failed)
    assert plan.outcome.kind == "failed"
    for markdown in (plan.review_comment_markdown, render_review_comment(plan, detail="full")):
        assert "审查因执行错误中断，已保留部分记录（部分审查记录）" in markdown
        assert "以下为执行失败前已保留的部分审查记录" in markdown
        # 不再复用正常审查的合并建议措辞。
        assert "部分问题尚未核实" not in markdown
        assert "审查因执行错误中断，以下建议不构成完整审查结论" in markdown
        assert [line for line in markdown.splitlines() if line.startswith("## ")] == [
            "## 已保留记录", "## 未覆盖范围", "## 重新审查"]
    assert "部分审查记录" in plan.summary_markdown
    assert "部分审查记录" not in PublicationPlanner().build(completed_task()).review_comment_markdown

    cancelled = publishable_task()
    cancelled.status = "cancelled"
    cancelled.task_runtime_report["execution_status"] = "cancelled"
    plan = PublicationPlanner().build(cancelled)
    assert plan.outcome.kind == "cancelled"
    for markdown in (plan.review_comment_markdown, render_review_comment(plan, detail="full")):
        assert "审查已停止，已保留停止前的记录（部分审查记录）" in markdown
        assert "以下为用户停止前已保留的部分审查记录" in markdown
        assert "审查已由用户停止，以下建议仅基于停止前保留的记录" in markdown
        assert [line for line in markdown.splitlines() if line.startswith("## ")] == [
            "## 已保留记录", "## 未覆盖范围", "## 重新审查"]
