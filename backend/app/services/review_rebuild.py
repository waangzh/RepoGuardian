"""状态重建 —— 将 LangGraph 扁平字典状态恢复为 Pydantic 模型。

图节点间传递的 ReviewState 是 dict（TypedDict），所有嵌套模型被序列化为 dict。
此模块提供 rebuild_task_from_state() 将最终 state 逆向重建为 ReviewTask 聚合根。
"""

from datetime import datetime, timezone

from app.graph.state import ReviewState
from app.models.review import (
    AgentEvent,
    ChangedFile,
    ChangedLine,
    ContextSnippet,
    DiffHunk,
    PatchResult,
    ProjectProfile,
    PullRequestInfo,
    PullRequestRef,
    RepoSnapshot,
    ReviewMode,
    ReviewPhase,
    ReviewIssue,
    ReviewSummary,
    ReviewTask,
    TestRunResult,
    ValidationDelta,
    ValidationBackend,
    ValidationResult,
    ValidationSnapshot,
)


def rebuild_task_from_state(state: ReviewState) -> ReviewTask:
    """从图执行结束后的 ReviewState 重建完整的 ReviewTask 聚合根。"""
    return ReviewTask(
        id=state.get("task_id", ""),
        status=state.get("status", "completed"),
        phase=ReviewPhase(state.get("phase") or ReviewPhase.completed),
        pr_url=state.get("pr_url", ""),
        model=state.get("model"),
        mode=rebuild_review_mode(state.get("mode")),
        generate_patches=bool(state.get("generate_patches", False)),
        validation_backend=rebuild_validation_backend(state.get("validation_backend")),
        review=ReviewSummary(
            mode=rebuild_review_mode(state.get("mode")),
            status=state.get("status", "completed"),
            completed=state.get("status") in {"completed", "completed_with_warnings"},
        ),
        pr=rebuild_pr_info(state.get("pr_info") or {}),
        changed_files=rebuild_changed_files(state.get("changed_files") or []),
        issues=rebuild_issues(state.get("review_issues") or []),
        context_snippets=rebuild_context_snippets(state.get("context_snippets") or []),
        repo_snapshot=rebuild_repo_snapshot(state.get("project_meta") or {}),
        project_profile=rebuild_project_profile(state.get("project_profile")),
        static_results=rebuild_test_results(state.get("static_results") or []),
        validation_snapshots=rebuild_validation_snapshots(state.get("validation_snapshots") or []),
        validation_deltas=rebuild_validation_deltas(state.get("validation_deltas") or []),
        validation=rebuild_validation_results(state.get("validation_results") or []),
        patches=rebuild_patches(state.get("patches") or []),
        test_results=rebuild_test_results(state.get("test_results") or []),
        agent_events=rebuild_agent_events(state.get("agent_events") or []),
        human_request=state.get("human_request"),
        report_markdown=state.get("report_markdown"),
        warnings=list(state.get("warnings") or []),
        error=state.get("error"),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def rebuild_pr_info(data: dict) -> PullRequestInfo:
    """从 dict 重建 PullRequestInfo。"""
    base = data.get("base", {})
    head = data.get("head", {})
    return PullRequestInfo(
        owner=data.get("owner", ""),
        repo=data.get("repo", ""),
        number=data.get("number", 0),
        title=data.get("title", ""),
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


def rebuild_validation_backend(value: object) -> ValidationBackend:
    try:
        return ValidationBackend(value or ValidationBackend.none)
    except ValueError:
        return ValidationBackend.none


def rebuild_changed_files(data: list[dict]) -> list[ChangedFile]:
    """从 dict 列表重建 ChangedFile（含嵌套的 DiffHunk 和 ChangedLine）。"""
    result: list[ChangedFile] = []
    for file_data in data:
        hunks = []
        for hunk_data in file_data.get("hunks", []):
            added = [
                ChangedLine(line_no=line.get("line_no"), content=line.get("content", ""))
                for line in hunk_data.get("added_lines", [])
            ]
            removed = [
                ChangedLine(line_no=line.get("line_no"), content=line.get("content", ""))
                for line in hunk_data.get("removed_lines", [])
            ]
            hunks.append(DiffHunk(
                old_start=hunk_data.get("old_start", 0),
                old_length=hunk_data.get("old_length", 0),
                new_start=hunk_data.get("new_start", 0),
                new_length=hunk_data.get("new_length", 0),
                added_lines=added,
                removed_lines=removed,
            ))
        result.append(ChangedFile(
            file_path=file_data.get("file_path", ""),
            change_type=file_data.get("change_type", "modified"),
            additions=file_data.get("additions", 0),
            deletions=file_data.get("deletions", 0),
            hunks=hunks,
        ))
    return result


def rebuild_context_snippets(data: list[dict]) -> list[ContextSnippet]:
    """从 dict 列表重建 ContextSnippet。"""
    return [
        ContextSnippet(
            file=item.get("file", ""),
            start_line=item.get("start_line", 1),
            end_line=item.get("end_line", item.get("start_line", 1)),
            content=item.get("content", ""),
            relevance=item.get("relevance", "adjacent"),
            symbol=item.get("symbol"),
        )
        for item in data
    ]


def rebuild_repo_snapshot(data: dict) -> RepoSnapshot | None:
    """从 dict 重建 RepoSnapshot。"""
    if not data:
        return None
    return RepoSnapshot(
        language=data.get("language", "unknown"),
        framework=data.get("framework"),
        test_framework=data.get("test_framework"),
        total_files=data.get("total_files", 0),
    )


def rebuild_project_profile(data: dict | None) -> ProjectProfile | None:
    """从图状态恢复项目适配器的检测结果。"""
    return ProjectProfile.model_validate(data) if data else None


def rebuild_issues(data: list[dict]) -> list[ReviewIssue]:
    """从 dict 列表重建 ReviewIssue（使用 Pydantic model_validate）。"""
    return [ReviewIssue.model_validate(item) for item in data]


def rebuild_test_results(data: list[dict]) -> list[TestRunResult]:
    """从 dict 列表重建 TestRunResult。"""
    return [TestRunResult.model_validate(item) for item in data]


def rebuild_validation_snapshots(data: list[dict]) -> list[ValidationSnapshot]:
    """从图状态恢复三个验证阶段的快照。"""
    return [ValidationSnapshot.model_validate(item) for item in data]


def rebuild_validation_deltas(data: list[dict]) -> list[ValidationDelta]:
    """从图状态恢复阶段间的验证差异。"""
    return [ValidationDelta.model_validate(item) for item in data]


def rebuild_validation_results(data: list[dict]) -> list[ValidationResult]:
    """读取新的 API 验证结论；旧 state 没有该字段时保持空列表。"""
    return [ValidationResult.model_validate(item) for item in data]


def rebuild_patches(data: list[dict]) -> list[PatchResult]:
    """从 dict 列表重建 PatchResult。"""
    return [PatchResult.model_validate(item) for item in data]


def rebuild_agent_events(data: list[dict]) -> list[AgentEvent]:
    """从 dict 列表重建 AgentEvent。"""
    return [AgentEvent.model_validate(item) for item in data]
