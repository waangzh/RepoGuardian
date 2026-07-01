from datetime import datetime, timezone

from app.graph.state import ReviewState
from app.models.review import (
    AgentEvent,
    ChangedFile,
    ChangedLine,
    ContextSnippet,
    DiffHunk,
    PatchResult,
    PullRequestInfo,
    PullRequestRef,
    RepoSnapshot,
    ReviewIssue,
    ReviewTask,
    TestRunResult,
)


def rebuild_task_from_state(state: ReviewState) -> ReviewTask:
    return ReviewTask(
        id=state.get("task_id", ""),
        status=state.get("status", "completed"),
        pr_url=state.get("pr_url", ""),
        model=state.get("model"),
        pr=rebuild_pr_info(state.get("pr_info") or {}),
        changed_files=rebuild_changed_files(state.get("changed_files") or []),
        issues=rebuild_issues(state.get("review_issues") or []),
        context_snippets=rebuild_context_snippets(state.get("context_snippets") or []),
        repo_snapshot=rebuild_repo_snapshot(state.get("project_meta") or {}),
        static_results=rebuild_test_results(state.get("static_results") or []),
        patches=rebuild_patches(state.get("patches") or []),
        test_results=rebuild_test_results(state.get("test_results") or []),
        agent_events=rebuild_agent_events(state.get("agent_events") or []),
        report_markdown=state.get("report_markdown"),
        error=state.get("error"),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def rebuild_pr_info(data: dict) -> PullRequestInfo:
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


def rebuild_changed_files(data: list[dict]) -> list[ChangedFile]:
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
    if not data:
        return None
    return RepoSnapshot(
        language=data.get("language", "unknown"),
        framework=data.get("framework"),
        test_framework=data.get("test_framework"),
        total_files=data.get("total_files", 0),
    )


def rebuild_issues(data: list[dict]) -> list[ReviewIssue]:
    return [ReviewIssue.model_validate(item) for item in data]


def rebuild_test_results(data: list[dict]) -> list[TestRunResult]:
    return [TestRunResult.model_validate(item) for item in data]


def rebuild_patches(data: list[dict]) -> list[PatchResult]:
    return [PatchResult.model_validate(item) for item in data]


def rebuild_agent_events(data: list[dict]) -> list[AgentEvent]:
    return [AgentEvent.model_validate(item) for item in data]
