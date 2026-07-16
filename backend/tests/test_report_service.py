from app.models.review import ChangedFile, ReviewIssue, ReviewTask, TaskStatus
from app.services.report_service import ReportService


def test_report_with_no_issues() -> None:
    task = ReviewTask(id="t1", pr_url="https://github.com/o/r/pull/1", status=TaskStatus.completed)

    report = ReportService().generate(task)

    assert "未发现有明确证据的代码问题" in report


def test_report_with_issue() -> None:
    task = ReviewTask(
        id="t1",
        pr_url="https://github.com/o/r/pull/1",
        status=TaskStatus.completed,
        changed_files=[
            ChangedFile(file_path="app.py", change_type="modified", additions=2, deletions=1)
        ],
        issues=[
            ReviewIssue(
                file_path="app.py",
                line_no=2,
                severity="high",
                category="correctness",
                title="可能空值异常",
                description="缺少空值处理。",
                suggestion="增加显式校验。",
                confidence=0.8,
                evidence="第 2 行缺少空值保护。",
                evidence_locations=[{"file_path": "app.py", "line_no": 2}],
                affected_behavior="空输入可能引发异常。",
            )
        ],
    )

    report = ReportService().generate(task)

    assert "可能空值异常" in report
    assert "app.py:2" in report
