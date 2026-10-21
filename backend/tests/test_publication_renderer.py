import pytest

from app.services.publication_planner import PublicationPlanner
from app.models.publication import PublicationPlan
from app.services.publication_renderer import markdown_text, render_finding, render_summary, render_user_report, render_review_comment
from test_publication_planner import publishable_task
from test_review_outcome import completed_task


@pytest.mark.parametrize("task,label", [(completed_task(), "本次检查未发现需在合并前处理的问题"),
    (completed_task(scope_complete=False), "尚无法确认检查范围是否完整"), (publishable_task(), "发现 1 项改进建议，暂无高优先级问题")])
def test_rendered_counts_and_repeatability(task, label):
    plan = PublicationPlanner().build(task)
    assert label in plan.summary_markdown
    assert f"本轮 Diff 新增代码证据已确认的问题：{plan.outcome.confirmed_count}" in plan.summary_markdown
    assert plan.summary_markdown == render_summary(plan)
    assert plan.user_report_markdown == render_user_report(task, plan)
    if plan.inline_drafts:
        assert render_finding(plan.inline_drafts[0]) in plan.user_report_markdown


def test_untrusted_markdown_and_html_are_escaped_and_bounded():
    task = publishable_task(claim="<script>alert(1)</script> ![x](https://evil) @everyone\n# heading")
    plan = PublicationPlanner().build(task)
    text = plan.summary_markdown + plan.inline_drafts[0].body_markdown
    assert "<script>" not in text and "![x]" not in text and "@everyone" not in text
    assert "&lt;script&gt;" in text and "&#64;everyone" in text
    assert len(markdown_text("x" * 10000, 100)) == 101
    assert "\u202e" not in markdown_text("a\u202eb")


@pytest.mark.parametrize("placement", ["inline", "summary"])
@pytest.mark.parametrize("claim", [
    "修复 config[0] 的校验问题",
    r"保留 C:\config\[0] 与 `key`、*value* 的原文",
    '<script>alert(1)</script> ![x](https://evil) @everyone &lt; & "value"',
    "x" * 220 + "[0]",
])
def test_titles_remain_plain_text_and_are_escaped_once_at_markdown_boundaries(placement, claim):
    task = publishable_task(placement=placement, claim=claim)
    plan = PublicationPlanner().build(task)
    finding = (plan.inline_drafts + plan.summary_findings)[0]
    assert finding.raw_title == finding.title == claim
    escaped = markdown_text(claim, 200)
    summary = f"1. [Medium] {escaped} — "
    heading = f"### {escaped}\n"
    comment = f"**[Medium · Correctness] {escaped}**"
    assert summary in plan.summary_markdown
    assert heading in plan.user_report_markdown
    assert comment in finding.body_markdown
    for detail in ("compact", "full"):
        assert comment in render_review_comment(plan, detail=detail)
    restored = PublicationPlan.model_validate_json(plan.model_dump_json())
    assert restored == plan
    assert heading in render_user_report(task, restored)


def test_legacy_titles_are_not_escaped_again_and_frozen_bodies_are_unchanged():
    task = publishable_task(claim=r"修复 config[0] 与 C:\config 的校验问题")
    saved = PublicationPlanner().build(task).model_dump(mode="json")
    finding = saved["inline_drafts"][0]
    finding.pop("raw_title")
    finding["title"] = markdown_text(finding["title"], 200)
    restored = PublicationPlan.model_validate(saved)
    assert restored.inline_drafts[0].raw_title is None
    for field in ("summary_markdown", "user_report_markdown", "review_comment_markdown"):
        assert getattr(restored, field) == saved[field]
    assert restored.inline_drafts[0].body_markdown == finding["body_markdown"]
    assert f"### {finding['title']}\n" in render_user_report(task, restored)
    assert f"**[Medium · Correctness] {finding['title']}**" in render_review_comment(restored)
    assert f"1. [Medium] {finding['title']} — " in render_summary(restored)


def test_digest_is_based_on_goals_not_requested_focus_and_escapes_reasons():
    task = completed_task(initial_scope_coverage=[
        {"goal_id": "g1", "target_ref": "src/a.py", "target_kind": "base_source_range", "start_line": 2,
         "end_line": 4, "status": "examined", "reason": "删除前契约核对"},
        {"goal_id": "g2", "target_ref": "src/b.py", "status": "not_examined", "reason": "<script>@everyone"}],
        scope_complete=False, termination_reason="budget_exhausted")
    from app.models.review import ExcludedReviewFile
    task.excluded_files = [ExcludedReviewFile(file_path="secret.env", reason="sensitive")]
    plan = PublicationPlanner().build(task)
    assert plan.digest.reviewed_files == ["src/a.py"]
    assert plan.digest.checked_areas == ["base_source_ranges"]
    assert len(plan.digest.incomplete_goals) == 1
    text = plan.user_report_markdown
    assert "src/a" in text and "2–4（base）" in text and "budget_exhausted" in text
    assert "secret" in text and "sensitive" in text and "可生成草稿" not in text
    assert "<script>" not in text and "@everyone" not in text


def test_complete_scope_with_unknown_usage_explains_reliability_separately():
    plan = PublicationPlanner().build(completed_task(usage_integrity="unknown"))
    assert plan.outcome.kind == "inconclusive"
    assert plan.outcome.review_completeness == "complete"
    assert plan.outcome.runtime_reliability == "unreliable"
    assert "计量不完整" in plan.summary_markdown and "部分目标未完成检查" not in plan.summary_markdown
    assert plan.outcome.coverage == "complete"
    assert plan.digest.merge_decision.status == "conditional_merge"
    assert plan.digest.merge_decision.label == "已检查范围内未发现已确认问题"
    for markdown in (plan.summary_markdown, plan.user_report_markdown, plan.review_comment_markdown):
        assert "部分问题尚未核实" not in markdown
        assert "先补齐“审查局限”中的证据缺口" not in markdown
        assert "运行或计量异常本身不代表发现代码缺陷" in markdown


@pytest.mark.parametrize("updates,label", [
    ({"scope_complete": False}, "尚无法确认检查范围是否完整"),
    ({"initial_scope_coverage": []}, "检查范围尚不明确"),
    ({"findings": [{"status": "unresolved"}]}, "有 1 项疑点需要进一步确认"),
    ({"admitted_investigations": [{"status": "not_executed"}]}, "有 1 项关联调查尚未完成"),
    ({"unscheduled_leads": [{"status": "not_scheduled"}]}, "存在未确认事项，需进一步核实"),
    ({"termination_reason": "budget_exhausted"}, "审查已结束，但结果可靠性仍需核对"),
    ({"scope_complete": False, "usage_integrity": "unknown"}, "尚无法确认检查范围是否完整"),
    ({"usage_integrity": "unknown", "admitted_investigations": [{"status": "not_executed"}]},
     "有 1 项关联调查尚未完成"),
])
def test_incomplete_labels_follow_saved_cause_without_changing_outcome(updates, label):
    plan = PublicationPlanner().build(completed_task(**updates))
    assert plan.digest.merge_decision.status == "resolve_unknowns"
    assert plan.digest.merge_decision.label == label
    assert label in plan.review_comment_markdown
    assert plan.outcome.kind == "inconclusive"


def test_unresolved_candidates_and_investigations_do_not_inflate_issue_count():
    plan = PublicationPlanner().build(completed_task(
        findings=[{"status": "unresolved"}, {"status": "candidate"}],
        admitted_investigations=[{"status": "not_executed"}] * 3))
    assert plan.digest.merge_decision.label == "有 2 项疑点需要进一步确认"
    assert plan.outcome.incomplete_investigations == 3


def test_historical_verified_defect_never_reads_as_no_confirmed_problem():
    """历史复核确认既有缺陷时，通用说明必须区分“本轮未新增”与“历史仍存在”。"""
    task = completed_task()
    task.pr_discussion = {
        "stats": {"status": "complete", "collected": 1, "comments": 1, "referenced": 1,
                  "referenced_comments": 1, "checked": 1, "unchecked": 0, "limitations": [],
                  "termination_reason": "plan_exhausted"},
        "rechecks": [{"discussion_id": "inline:1", "path": "src/a.py", "status": "still_present",
                      "reason": "当前实现仍返回空值", "evidence_ids": ["e1"], "limitations": []}]}
    plan = PublicationPlanner().build(task)
    assert plan.outcome.kind == "clean"
    assert plan.outcome.coverage == "complete" and plan.outcome.reason_codes == []
    assert plan.digest.merge_decision.status == "resolve_unknowns"
    assert "historical_discussion_unresolved" in plan.digest.merge_decision.unresolved_reasons
    assert plan.digest.merge_decision.label == "历史讨论中的问题经代码核验仍存在，需先核对"
    for markdown in (plan.summary_markdown, plan.user_report_markdown, plan.review_comment_markdown):
        assert "目前没有可确认的问题" not in markdown
        assert "历史讨论复核确认 1 项既有问题在当前代码中仍存在" in markdown
    assert "本轮变更范围内未新增代码证据已确认的问题" in plan.user_report_markdown
    assert "本轮 Diff 新增代码证据已确认的问题 0 项" in plan.user_report_markdown
    assert "本轮 Diff 新增代码证据已确认的问题：0" in plan.summary_markdown
    assert "已读取并保存 1 条 PR 评论，整理为 1 项讨论" in plan.user_report_markdown
    assert "复核结论含代码核对结果和背景说明" in plan.user_report_markdown


def test_new_findings_and_verified_history_are_both_disclosed():
    from test_review_outcome import discussion

    task = publishable_task()
    task.pr_discussion = discussion(rechecks=[
        {"discussion_id": "inline:1", "path": "src/old.py", "status": "still_present",
         "reason": "已有调用仍缺少权限检查", "evidence_ids": ["history-e1"], "limitations": []}])
    plan = PublicationPlanner().build(task)
    assert plan.outcome.kind == "findings" and plan.outcome.coverage == "complete"
    assert plan.outcome.reason_codes == []
    assert plan.digest.merge_decision.status == "resolve_unknowns"
    assert "本轮变更范围内发现 1 项代码证据已确认的问题" in plan.user_report_markdown
    assert "本轮变更范围内未新增代码证据已确认的问题" not in plan.user_report_markdown
    for markdown in (plan.summary_markdown, plan.user_report_markdown, plan.review_comment_markdown):
        assert "已有调用仍缺少权限检查" in markdown
        assert "空返回值破坏调用方契约" in markdown


def test_summary_keeps_material_gaps_without_runtime_and_file_audit():
    from app.models.review import ExcludedReviewFile

    task = completed_task(initial_scope_coverage=[
        {"goal_id": "g1", "target_ref": "src/checked.py", "target_kind": "base_source_range",
         "status": "examined", "reason": "静态检查完成"},
        {"goal_id": "g2", "target_ref": "src/pending.py", "status": "not_examined",
         "reason": "budget_exhausted"}], scope_complete=False, termination_reason="budget_exhausted",
        usage_integrity="unknown", findings=[{"status": "unresolved"}])
    task.excluded_files = [ExcludedReviewFile(file_path="excluded.bin", reason="binary")]
    plan = PublicationPlanner().build(task)
    text = plan.summary_markdown
    for phrase in ("1 项约定目标未完成检查", "1 项候选问题尚未确认", "计量不完整", "### 下一步",
                   "1 个文件未纳入本轮审查", plan.snapshot.head_sha):
        assert phrase in text
    for phrase in ("Base 删除前源码区间", "src/checked", "src/pending", "budget_exhausted",
                   "scope_incomplete", "usage_integrity_unknown", "运行终止记录", "可信绑定", "excluded.bin"):
        assert phrase not in text
    assert "src/pending" in plan.user_report_markdown
    assert "budget_exhausted" in plan.user_report_markdown
    assert text.count("未执行目标仓库代码、测试或构建") == 1


def test_summary_preserves_actionable_summary_only_findings_and_inline_links():
    from copy import deepcopy

    task = publishable_task()
    summary = deepcopy(publishable_task(placement="summary", candidate_id="summary-b",
                                       claim="缓存失效后读取失败").task_runtime_report["findings"][0])
    summary["key"] = "key-b"
    task.task_runtime_report["findings"].append(summary)
    plan = PublicationPlanner().build(task)
    before = plan.model_dump(mode="json")
    url = task.pr_url + "#discussion_r123"
    text = render_summary(plan, discussion_urls={"key-a": url})
    assert text.index("### 问题索引") < text.index("### 下一步")
    assert f"[查看 Inline 讨论]({url})" in text
    assert "缓存失效后读取失败" in text
    for phrase in ("调用方字段读取失败", "服务返回空值时直接读取字段", "先处理空值"):
        assert phrase in text
    assert plan.model_dump(mode="json") == before


def test_bound_evidence_text_is_escaped_without_copying_raw_code():
    task = publishable_task()
    record = task.task_runtime_report["findings"][0]
    record["verification"]["reason"] = record["publication"]["basis"] = "<script> ![x](https://evil) @everyone"
    finding = PublicationPlanner().build(task).inline_drafts[0]
    assert "<script>" not in finding.body_markdown and "![x]" not in finding.body_markdown
    assert "@everyone" not in finding.body_markdown and "&#64;everyone" in finding.body_markdown
