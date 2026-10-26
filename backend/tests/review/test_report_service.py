from app.models.review import ReviewTask
from app.services.review.report_service import ReportService
from app.models.review_runtime import TaskRuntimeReport


def test_runtime_user_report_contains_actionable_details_and_separate_audit() -> None:
    runtime = TaskRuntimeReport(execution_status="completed", termination_reason="plan_exhausted", scope_complete=True,
        usage_integrity="unknown", unknown_attempts=1, initial_scope_coverage=[{"status": "examined"}],
        admitted_investigations=[{"status": "insufficient_evidence", "reason": "调查依据", "limitations": ["仅静态核对"]}],
        findings=[{"status": "confirmed", "candidate": {"claim": "原始主张"}, "issue": {
            "title": "核验后的标题", "affected_behavior": "受影响的行为", "failure_scenario": "可复现的失败场景",
            "recommendation": "核验后的修改建议", "primary_evidence": {"file_path": "src/stats.py", "resolved_start_line": 12, "resolved_side": "base"}}},
            {"status": "unresolved", "candidate": {"claim": "待确认的业务约定"}, "issue": None, "reason": "candidate_derivation_incomplete"},
            {"status": "dismissed", "candidate": {"claim": "已排除的误报"}}])
    task = ReviewTask(id="runtime", engine="task_review_v1", pr_url="https://github.com/o/r/pull/1", task_runtime_report=runtime.model_dump(mode="json"))
    markdown = ReportService().generate(task)
    assert "## 快照与结论身份" in markdown and "## 运行、计量与证据追踪" in markdown
    for value in ("核验后的标题", "受影响的行为", "可复现的失败场景", "核验后的修改建议",
                  "原始主张", "candidate_derivation_incomplete", "调查依据", "仅静态核对", "task-goals-v1", "已排除的误报"):
        assert value in markdown
    assert "## PR 变更理解" not in markdown and "## 发现的问题" not in markdown


def test_runtime_report_escapes_model_html_and_keeps_missing_report_unknown() -> None:
    runtime = TaskRuntimeReport(execution_status="failed", termination_reason="technical_failure", usage_integrity="unknown",
        findings=[{"status": "unresolved", "candidate": {"claim": "</details><script>alert(1)</script>"}}])
    task = ReviewTask(id="runtime", engine="task_review_v1", pr_url="https://github.com/o/r/pull/1", task_runtime_report=runtime.model_dump(mode="json"))
    markdown = ReportService().generate(task)
    assert "<script>" not in markdown and "&lt;script&gt;" in markdown
    assert "failure_summary" in markdown and "审查完整性：未知" in markdown
    task.task_runtime_report = None
    assert "报告尚未生成" in ReportService().generate(task)
