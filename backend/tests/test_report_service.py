from app.models.review import (
    ChangedFile,
    PullRequestInfo,
    PullRequestRef,
    ReviewIssue,
    ReviewTask,
    TaskStatus,
)
from app.services.report_service import ReportService


def _pr(*, body: str | None = None) -> PullRequestInfo:
    return PullRequestInfo(
        owner="o",
        repo="r",
        number=1,
        title="fix(stats): keep the point estimate inside its interval",
        body=body,
        html_url="https://github.com/o/r/pull/1",
        clone_url="https://github.com/o/r.git",
        base=PullRequestRef(ref="main", sha="a" * 40, repo_clone_url="https://github.com/o/r.git"),
        head=PullRequestRef(ref="fix", sha="b" * 40, repo_clone_url="https://github.com/o/r.git"),
    )


def test_report_with_no_issues() -> None:
    task = ReviewTask(id="t1", pr_url="https://github.com/o/r/pull/1", status=TaskStatus.completed)

    report = ReportService().generate(task)

    assert "未发现有明确证据的代码问题" in report
    assert "## 详细问题" not in report
    assert "## 静态分析结果" not in report
    assert "## 验证结果" not in report
    assert "## 候选修复结果" not in report
    assert "## 测试结果" not in report
    assert "未运行。" not in report
    assert "未请求验证。" not in report
    assert "未生成 patch。" not in report
    assert "<summary>执行详情（Agent 决策与任务元数据）</summary>" in report


def test_report_explains_pr_purpose_from_body_and_diff_scope() -> None:
    task = ReviewTask(
        id="t1",
        pr_url="https://github.com/o/r/pull/1",
        status=TaskStatus.completed,
        pr=_pr(body="## Why\n\nEnsure interval statistics always contain their point estimate."),
        changed_files=[
            ChangedFile(
                file_path="src/stats.py",
                change_type="modified",
                additions=17,
                deletions=0,
            ),
            ChangedFile(
                file_path="tests/test_stats.py",
                change_type="modified",
                additions=34,
                deletions=0,
            ),
        ],
    )

    report = ReportService().generate(
        task,
        purpose_summary="确保区间统计结果始终包含其点估计，并补充对应测试覆盖。",
    )

    assert "## PR 作用" in report
    assert "**作者意图（中文概括）：** 确保区间统计结果始终包含其点估计" in report
    assert "Ensure interval statistics" not in report
    assert "1 个实现或配置文件、1 个测试文件" in report
    assert "新增 51 行、删除 0 行" in report
    assert "PR 未提供正文" not in report
    assert "## 1." not in report


def test_report_labels_title_based_purpose_and_preserves_warnings() -> None:
    task = ReviewTask(
        id="t1",
        pr_url="https://github.com/o/r/pull/1",
        status=TaskStatus.completed_with_warnings,
        pr=_pr(),
        warnings=["静态分析后端不可用"],
    )

    report = ReportService().generate(task)
    purpose_section = report.split("## PR 作用", 1)[1].split("## 审查结论", 1)[0]

    assert "**作用概括：** 未获得可靠的中文作者说明" in report
    assert "keep the point estimate inside its interval" not in purpose_section
    assert "未直接复述非中文 PR 标题或正文" in report
    assert "## 审查限制与警告" in report
    assert "静态分析后端不可用" in report


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
                review_unit_id="unit-1",
                severity="high",
                category="correctness",
                title="可能空值异常",
                failure_scenario="缺少空值处理。",
                recommendation="增加显式校验。",
                confidence=0.8,
                primary_evidence={
                    "file_path": "app.py",
                    "existing_code": "value = item.name",
                    "resolved_start_line": 2,
                    "resolved_end_line": 2,
                    "resolution_method": "file_exact",
                    "match_count": 1,
                    "resolved_side": "head",
                },
                placement="summary",
                status="evidence_resolved",
                affected_behavior="空输入可能引发异常。",
            )
        ],
    )

    report = ReportService().generate(task)

    assert "可能空值异常" in report
    assert "app.py:2" in report
    assert "## 详细问题" in report
