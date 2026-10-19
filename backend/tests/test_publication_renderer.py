import pytest

from app.services.publication_planner import PublicationPlanner
from app.services.publication_renderer import markdown_text, render_finding, render_summary, render_user_report
from test_publication_planner import publishable_task
from test_review_outcome import completed_task


@pytest.mark.parametrize("task,label", [(completed_task(), "未发现阻断性问题"),
    (completed_task(scope_complete=False), "需要补充检查"), (publishable_task(), "建议评估后合并")])
def test_rendered_counts_and_repeatability(task, label):
    plan = PublicationPlanner().build(task)
    assert label in plan.summary_markdown
    assert f"已确认问题：{plan.outcome.confirmed_count}" in plan.summary_markdown
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


def test_bound_evidence_text_is_escaped_without_copying_raw_code():
    task = publishable_task()
    record = task.task_runtime_report["findings"][0]
    record["verification"]["reason"] = record["publication"]["basis"] = "<script> ![x](https://evil) @everyone"
    finding = PublicationPlanner().build(task).inline_drafts[0]
    assert "<script>" not in finding.body_markdown and "![x]" not in finding.body_markdown
    assert "@everyone" not in finding.body_markdown and "&#64;everyone" in finding.body_markdown
