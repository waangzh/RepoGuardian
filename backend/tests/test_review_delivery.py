from copy import deepcopy
from datetime import datetime, timezone

import pytest

from app.models.review import ChangedFile, DiffHunk, DiffLine
from app.services.publication_planner import PublicationPlanner
from app.services.report_service import ReportService
from app.services.publication_renderer import render_user_report
from test_publication_planner import publishable_task
from test_report_service import _pr


def test_comment_contains_inline_finding_position_and_immutable_base_link():
    task = publishable_task(side="base", severity="high", claim="核心功能鉴权失败")
    task.task_runtime_report["findings"][0]["issue"]["confidence"] = 0.9
    plan = PublicationPlanner().build(task)
    finding = plan.inline_drafts[0]
    # 评论是发给作者的审查意见：标题、位置与建议必须保留，但不复制完整审计正文。
    assert finding.title in plan.review_comment_markdown
    assert finding.recommendation in plan.review_comment_markdown
    # 评论中的行内定位仍链接到不可变的 Effective Base 快照（URL 路径不做 Markdown 转义）。
    assert f"/blob/{task.run_manifest.effective_base_sha}/src/a.py#L7" in plan.review_comment_markdown
    assert "发现 1 项高优先级问题，建议修复后复审" in plan.review_comment_markdown and "CI" in plan.review_comment_markdown
    assert plan.review_comment_markdown.count("本结论基于提交") == 1
    assert plan.user_report_markdown.index("## 代码证据已确认的问题") < plan.user_report_markdown.index("## 附录：审查范围与限制")


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
    audit = ReportService().generate(task)
    assert base.user_report_markdown.split("\n", 1)[1] not in audit
    assert "运行、计量与证据追踪" in audit and "key-a" in audit


def test_author_statement_and_actual_diff_are_kept_distinct_from_alignment():
    task = publishable_task()
    task.pr = _pr(body="<!--隐藏-->修复空值异常 <script>@everyone</script>")
    task.changed_files = [ChangedFile(file_path="src/a.py", change_type="modified", additions=1, deletions=0,
        hunks=[DiffHunk(old_start=1, old_length=1, new_start=1, new_length=2,
            lines=[DiffLine(kind="added", content="def handle(value):", new_line_no=1)])])]
    plan = PublicationPlanner().build(task)
    assert "handle" in plan.user_report_markdown
    assert "**实现变化：**" in plan.review_comment_markdown
    assert "尚未形成可引用的实现行为观察" in plan.review_comment_markdown
    assert "修复空值异常" in plan.review_comment_markdown
    assert "未保存独立的目标符合性核验记录" in plan.user_report_markdown
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


def test_zero_finding_report_and_comment_explain_behavior_and_answered_investigation():
    from test_review_outcome import completed_task
    task = completed_task(initial_scope_coverage=[{"goal_id": "g", "target_ref": "src/a.py",
        "target_kind": "source_range", "start_line": 3, "end_line": 8, "status": "examined",
        "reason": "检查空值分支先返回，再读取非空对象的字段"}], admitted_investigations=[{
        "proposition": "调用方是否继续消费已删除字段", "status": "answered", "answer": "refuted",
        "reason": "调用方已切换到新的返回结构", "limitations": ["仅核对固定快照，未执行测试"]}])
    plan = PublicationPlanner().build(task)
    for markdown in (plan.review_comment_markdown, plan.user_report_markdown):
        assert "检查空值分支先返回" in markdown
    # 详细报告保留已回答调查与限制；评论只列影响决策的未决事项。
    assert "调用方已切换到新的返回结构" in plan.user_report_markdown
    assert "未执行测试" in plan.user_report_markdown and "吻合与未确认" in plan.user_report_markdown
    # 评论只保留可读的变更概述，不再展开三问式作者意图对照。
    assert "作者想解决什么" not in plan.review_comment_markdown
    # 渲染器消费同一已构建事实，不再从原始运行数据另行判定调查状态。
    task.task_runtime_report["admitted_investigations"] = []
    assert render_user_report(task, plan) == plan.user_report_markdown


def test_compact_change_understanding_summarizes_and_does_not_trim_defect():
    task = publishable_task()
    task.changed_files = [ChangedFile(file_path=f"src/file{i}.py", change_type="modified", additions=1, deletions=1)
                          for i in range(8)]
    plan = PublicationPlanner().build(task)
    assert "**变更范围（Diff 元数据）：**" in plan.review_comment_markdown
    assert "共变更 8 个文件" in plan.review_comment_markdown
    assert "src/file7" not in plan.review_comment_markdown
    assert "src/file7" in plan.user_report_markdown
    finding = plan.inline_drafts[0]
    assert finding.title in plan.review_comment_markdown
    assert finding.recommendation in plan.review_comment_markdown


def test_unknown_investigation_answer_cannot_become_a_supported_change_claim():
    from test_review_outcome import completed_task
    task = completed_task(admitted_investigations=[{"proposition": "边界是否保持兼容", "status": "answered",
        "answer": ["supported"], "reason": "无效答案", "limitations": []}])
    plan = PublicationPlanner().build(task)
    assert plan.digest.investigations[0].answer == "unresolved"
    assert "静态证据支持此命题" not in plan.review_comment_markdown
    assert "未决调查：边界是否保持兼容" in plan.user_report_markdown
