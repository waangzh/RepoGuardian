import pytest

from app.services.publication_planner import PublicationPlanner
from app.services.publication_renderer import markdown_text, render_finding, render_summary, render_user_report
from test_publication_planner import publishable_task
from test_review_outcome import completed_task


@pytest.mark.parametrize("task,label", [(completed_task(), "未发现可行动问题"),
    (completed_task(scope_complete=False), "审查结论不确定"), (publishable_task(), "发现已确认问题")])
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
