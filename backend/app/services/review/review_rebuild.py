"""状态重建 —— 将 LangGraph 扁平字典状态恢复为 Pydantic 模型。

图节点间传递的 ReviewState 是 dict（TypedDict），所有嵌套模型被序列化为 dict。
此模块提供 rebuild_task_from_state() 将最终 state 逆向重建为 ReviewTask 聚合根。
"""

from datetime import datetime, timezone

from app.graph.state import ReviewState
from app.models.review import (
    AgentEvent,
    ChangedFile,
    ContextSnippet,
    ProjectProfile,
    PullRequestInfo,
    PullRequestRef,
    RepoSnapshot,
    ModelUsage,
    ReviewMode,
    ReviewPhase,
    ReviewIssue,
    ReviewUnit,
    ReviewUnitResult,
    CrossUnitRiskAssessment,
    CrossUnitCoordinationPlan,
    CrossUnitFollowupResult,
    ReviewCoverage,
    ReviewRunManifest,
    ExcludedReviewFile,
    IssueMetrics,
    IssueStatus,
    ReviewSummary,
    ReviewTask,
)
from app.llm.model_usage import summarize_model_usage


def rebuild_task_from_state(state: ReviewState) -> ReviewTask:
    """从图执行结束后的 ReviewState 重建完整的 ReviewTask 聚合根。"""
    now = datetime.now(timezone.utc)
    model_usages = _rebuild_model_usages(state)
    return ReviewTask(
        id=state.get("task_id", ""),
        status=state.get("status", "completed"),
        phase=ReviewPhase(state.get("phase") or ReviewPhase.completed),
        pr_url=state.get("pr_url", ""),
        model=state.get("model"),
        engine=(state.get("engine") or "legacy_units"),
        task_runtime_configuration=state.get("task_runtime_configuration"),
        incremental=(state.get("task_runtime_configuration") or {}).get("incremental"),
        task_runtime_report=state.get("task_runtime_report"),
        pr_discussion=state.get("pr_discussion"),
        task_runtime_retry_unit_id=state.get("task_runtime_retry_unit_id"),
        mode=rebuild_review_mode(state.get("mode")),
        review=ReviewSummary(
            mode=rebuild_review_mode(state.get("mode")),
            status=state.get("status", "completed"),
            completed=state.get("status") in {"completed", "completed_with_warnings"},
        ),
        pr=rebuild_pr_info(state.get("pr_info") or {}),
        changed_files=rebuild_changed_files(state.get("changed_files") or []),
        review_units=[ReviewUnit.model_validate(item) for item in state.get("review_units") or []],
        review_unit_results=rebuild_review_unit_results(state.get("review_unit_results") or []),
        cross_unit_risk=(CrossUnitRiskAssessment.model_validate(state["cross_unit_risk"])
                         if state.get("cross_unit_risk") else None),
        coordination_plan=(CrossUnitCoordinationPlan.model_validate(state["coordination_plan"])
                           if state.get("coordination_plan") else None),
        followup_results=[CrossUnitFollowupResult.model_validate(item)
                          for item in state.get("followup_results") or []],
        model_usages=model_usages,
        model_usage_summary=summarize_model_usage(model_usages),
        excluded_files=[
            ExcludedReviewFile.model_validate(item) for item in state.get("excluded_files") or []
        ],
        issues=rebuild_issues(state.get("review_issues") or []),
        issue_metrics=IssueMetrics.model_validate(state.get("issue_metrics") or {}),
        context_snippets=rebuild_context_snippets(state.get("context_snippets") or []),
        repo_snapshot=rebuild_repo_snapshot(state.get("project_meta") or {}),
        project_profile=rebuild_project_profile(state.get("project_profile")),
        coverage=ReviewCoverage.model_validate(state.get("review_coverage") or {}),
        run_manifest=(
            ReviewRunManifest.model_validate(state["run_manifest"])
            if state.get("run_manifest") else None
        ),
        agent_events=rebuild_agent_events(state.get("agent_events") or []),
        human_request=state.get("human_request"),
        report_markdown=state.get("report_markdown"),
        warnings=list(state.get("warnings") or []),
        error=state.get("error"),
        created_at=_rebuild_datetime(state.get("created_at"), now),
        updated_at=_rebuild_datetime(state.get("updated_at"), now),
    )


def _rebuild_model_usages(state: ReviewState) -> list[ModelUsage]:
    raw_items = list(state.get("model_usages") or [])
    for raw_result in state.get("review_unit_results") or []:
        raw_items.extend(raw_result.get("model_usages") or [])
    for raw_result in state.get("followup_results") or []:
        raw_items.extend((raw_result.get("unit_result") or {}).get("model_usages") or [])
    by_id: dict[str, ModelUsage] = {}
    for raw in raw_items:
        usage = ModelUsage.model_validate(raw)
        by_id[usage.id] = usage
    return list(by_id.values())


def _rebuild_datetime(value: object, default: datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return default


def rebuild_pr_info(data: dict) -> PullRequestInfo:
    """从 dict 重建 PullRequestInfo。"""
    base = data.get("base", {})
    head = data.get("head", {})
    return PullRequestInfo(
        owner=data.get("owner", ""),
        repo=data.get("repo", ""),
        number=data.get("number", 0),
        title=data.get("title", ""),
        body=data.get("body"),
        html_url=data.get("html_url", ""),
        clone_url=data.get("clone_url", ""),
        base=PullRequestRef(
            ref=base.get("ref", ""),
            sha=base.get("sha", ""),
            repo_clone_url=base.get("repo_clone_url", ""),
        ),
        head=PullRequestRef(
            ref=head.get("ref", ""),
            sha=head.get("sha", ""),
            repo_clone_url=head.get("repo_clone_url", ""),
        ),
    )


def rebuild_review_mode(value: object) -> ReviewMode:
    """将 pre-mode state 的 pr_review 读为新的安全默认模式。"""
    try:
        return ReviewMode(value or ReviewMode.review)
    except ValueError:
        return ReviewMode.review


def rebuild_changed_files(data: list[dict]) -> list[ChangedFile]:
    """按领域模型完整恢复文件、hunk、ordered diff 与二进制标记。"""
    return [ChangedFile.model_validate(item) for item in data]


def rebuild_context_snippets(data: list[dict]) -> list[ContextSnippet]:
    """按领域模型完整恢复检索片段及 provenance 字段。"""
    return [ContextSnippet.model_validate(item) for item in data]


def rebuild_repo_snapshot(data: dict) -> RepoSnapshot | None:
    """从 dict 重建 RepoSnapshot。"""
    if not data:
        return None
    return RepoSnapshot(
        language=data.get("language", "unknown"),
        languages=list(data.get("languages") or []),
        language_counts=dict(data.get("language_counts") or {}),
        is_mixed_language=bool(data.get("is_mixed_language", False)),
        framework=data.get("framework"),
        test_framework=data.get("test_framework"),
        total_files=data.get("total_files", 0),
    )


def rebuild_project_profile(data: dict | None) -> ProjectProfile | None:
    """从图状态恢复项目适配器的检测结果。"""
    return ProjectProfile.model_validate(data) if data else None


def rebuild_issues(data: list[dict]) -> list[ReviewIssue]:
    """公开已确认与未确认候选，保留证据和验证缺口。"""
    issues = [ReviewIssue.model_validate(item) for item in data]
    return [
        issue for issue in issues
        if issue.status in {IssueStatus.confirmed, IssueStatus.unresolved, IssueStatus.candidate}
    ]


def rebuild_review_unit_results(data: list[dict]) -> list[ReviewUnitResult]:
    """Unit 结果遵循与任务聚合根相同的发布边界。"""
    results: list[ReviewUnitResult] = []
    for item in data:
        result = ReviewUnitResult.model_validate(item)
        results.append(result.model_copy(update={
            "issues": [
                issue for issue in result.issues
                if issue.status in {IssueStatus.confirmed, IssueStatus.unresolved, IssueStatus.candidate}
            ],
        }))
    return results


def rebuild_agent_events(data: list[dict]) -> list[AgentEvent]:
    """从 dict 列表重建 AgentEvent。"""
    return [AgentEvent.model_validate(item) for item in data]
