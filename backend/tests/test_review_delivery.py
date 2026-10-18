from copy import deepcopy
from datetime import datetime, timezone

import pytest

from app.models.review import ChangedFile, DiffHunk, DiffLine
from app.services.publication_planner import PublicationPlanner
from app.services.report_service import ReportService
from test_publication_planner import publishable_task
from test_report_service import _pr


def test_comment_contains_full_inline_body_position_and_immutable_base_link():
    task = publishable_task(side="base", severity="high")
    plan = PublicationPlanner().build(task)
    assert plan.inline_drafts[0].body_markdown in plan.review_comment_markdown
    assert f"/blob/{task.run_manifest.effective_base_sha}/src/a.py#L7" in plan.review_comment_markdown
    assert "结合影响、CI" in plan.review_comment_markdown  # Issue 策略可降低原始严重度。
    assert "审查 Commit" in plan.review_comment_markdown
    assert plan.user_report_markdown.index("## 发现的问题") < plan.user_report_markdown.index("## 审查范围与限制")


def test_detailed_and_audit_share_facts_and_human_state_does_not_change_admission():
    task = publishable_task()
    original = deepcopy(task.task_runtime_report)
    saved = {"key-a": {"current": {"status": "resolved", "reason": "已补充空值保护"},
                       "updated_at": datetime(2026, 10, 19, tzinfo=timezone.utc)}}
    base = PublicationPlanner().build(task)
    plan = PublicationPlanner().build(task, dispositions=saved)
    assert plan.outcome == base.outcome and plan.inline_drafts == base.inline_drafts
    assert "人工处理：已处理" in plan.user_report_markdown
    assert r"2026\-10\-19" in plan.review_comment_markdown
    assert plan.summary_markdown == base.summary_markdown
    assert task.task_runtime_report == original
    assert base.user_report_markdown.split("\n", 1)[1] in ReportService().generate(task)


def test_author_statement_and_actual_diff_are_kept_distinct_from_alignment():
    task = publishable_task()
    task.pr = _pr(body="<!--隐藏-->修复空值异常 <script>@everyone</script>")
    task.changed_files = [ChangedFile(file_path="src/a.py", change_type="modified", additions=1, deletions=0,
        hunks=[DiffHunk(old_start=1, old_length=1, new_start=1, new_length=2,
            lines=[DiffLine(kind="added", content="def handle(value):", new_line_no=1)])])]
    plan = PublicationPlanner().build(task)
    assert "handle" in plan.user_report_markdown and "固定 Diff 事实" in plan.review_comment_markdown
    assert "未保存独立的目标符合性核验记录" in plan.review_comment_markdown
    assert "隐藏" not in plan.user_report_markdown and "<script>" not in plan.user_report_markdown


@pytest.mark.parametrize("state", ["unresolved", "dismissed", "candidate", "future"])
def test_non_publishable_records_are_visible_but_never_turn_into_comment_defects(state):
    task = publishable_task()
    task.task_runtime_report["findings"][0]["status"] = state
    plan = PublicationPlanner().build(task)
    assert "空返回值破坏调用方契约" in plan.user_report_markdown
    assert not plan.inline_drafts
    assert "**问题与影响：**" not in plan.review_comment_markdown


def test_tampered_publication_text_is_never_copied():
    task = publishable_task()
    task.task_runtime_report["findings"][0]["publication"]["published_text"]["claim"] = "篡改正文"
    plan = PublicationPlanner().build(task)
    assert "篡改正文" not in plan.review_comment_markdown
    assert "未通过发布准入" in plan.review_comment_markdown
