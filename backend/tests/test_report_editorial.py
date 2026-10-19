"""报告编辑性契约：评论面向作者、详细报告不重复、概览不携带审计数据。"""

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
    assert "拒绝空值" in understanding.author_goal
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
    assert compact.count("Commit") == 1
    assert "独立核验支持此结论" not in compact
    assert "静态审查 · 未执行目标代码" not in compact
    assert compact.index("回归令牌校验缺失") < compact.index("Commit")
    # 评论采用图片结构：标题、单行结论、变更概述、问题、其他审查结果、建议。
    assert compact.startswith("RepoGuardian · PR Code Review\n\n✅ 结论：未发现阻断性问题")
    for heading in ("## 变更概述", "## 发现的问题", "## 其他审查结果", "## 建议"):
        assert heading in compact
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
    assert report.count("### 验证边界") == 1
    assert report.index("## 附录：审查范围与限制") < report.index("### 验证边界")
    assert "src/b.py" not in report.split("## 附录")[0]
    assert report.count("仍以审查范围与验证边界为准") <= 1


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
    # 未核验不进入评论风险；明确矛盾才展示位置。
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
    assert "执行范围或验证记录" in bounded.digest.merge_decision.advice
    assert "审查预算耗尽，请补查未完成目标后再判断本次变更" in bounded.digest.merge_decision.limits
    assert "修复上方阻断问题" not in bounded.user_report_markdown
    clean = PublicationPlanner().build(completed_task())
    assert clean.digest.merge_decision.status == "conditional_merge"
    assert clean.digest.merge_decision.label == "未发现阻断性问题"
    assert clean.digest.merge_decision.limits == []
