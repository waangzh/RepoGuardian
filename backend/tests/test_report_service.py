from app.models.review import (
    ChangedFile,
    PullRequestInfo,
    PullRequestRef,
    ReviewIssue,
    ReviewTask,
    TaskStatus,
)
from app.services.report_service import ReportService
from app.models.review_runtime import TaskRuntimeReport


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
    user, audit = markdown.split("<summary>技术审计附录（运行、调查与关联证据）</summary>", 1)
    for value in ("核验后的标题", "受影响的行为", "可复现的失败场景", "核验后的修改建议", "尚无可靠代码定位", "尚未确认缺陷", "计量不完整"):
        assert value in user
    for value in ("原始主张", "candidate_derivation_incomplete"):
        assert value not in user
    assert "调查依据" in audit and "仅静态核对" in audit and "task-goals-v1" in audit
    assert "已排除的误报" in user and "机器已确认但未生成发布评论" in user


def test_runtime_report_escapes_model_html_and_keeps_missing_report_unknown() -> None:
    runtime = TaskRuntimeReport(execution_status="failed", termination_reason="technical_failure", usage_integrity="unknown",
        findings=[{"status": "unresolved", "candidate": {"claim": "</details><script>alert(1)</script>"}}])
    task = ReviewTask(id="runtime", engine="task_review_v1", pr_url="https://github.com/o/r/pull/1", task_runtime_report=runtime.model_dump(mode="json"))
    markdown = ReportService().generate(task)
    assert "<script>" not in markdown and "&lt;script&gt;" in markdown
    assert "失败原因" in markdown and "审查完整性：未知" in markdown
    task.task_runtime_report = None
    assert "报告尚未生成" in ReportService().generate(task)


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
