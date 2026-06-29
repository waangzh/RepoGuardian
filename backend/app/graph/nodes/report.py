from datetime import datetime, timezone

from app.graph.state import ReviewState
from app.models.review import ChangedFile, ChangedLine, PullRequestInfo, ReviewIssue, ReviewTask
from app.services.report_service import ReportService


async def report_node(state: ReviewState) -> ReviewState:
    task = _rebuild_task(state)
    report_service = ReportService()
    markdown = report_service.generate(task)

    step_progress: list[dict] = list(state.get("step_progress") or [])
    step_progress.append({
        "node": "report",
        "status": "completed",
        "message": "报告已生成",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return ReviewState(
        report_markdown=markdown,
        status="completed",
        step_progress=step_progress,
    )


def _rebuild_task(state: ReviewState) -> ReviewTask:
    pr_info_dict = state.get("pr_info") or {}
    pr = PullRequestInfo(
        owner=pr_info_dict.get("owner", ""),
        repo=pr_info_dict.get("repo", ""),
        number=pr_info_dict.get("number", 0),
        title=pr_info_dict.get("title", ""),
        html_url=pr_info_dict.get("html_url", ""),
        clone_url=pr_info_dict.get("clone_url", ""),
        base=_rebuild_ref(pr_info_dict.get("base", {})),
        head=_rebuild_ref(pr_info_dict.get("head", {})),
    )

    changed_files: list[ChangedFile] = []
    for f in state.get("changed_files") or []:
        hunks = []
        for h in f.get("hunks", []):
            added = [ChangedLine(line_no=a.get("line_no"), content=a.get("content", ""))
                     for a in h.get("added_lines", [])]
            removed = [ChangedLine(line_no=r.get("line_no"), content=r.get("content", ""))
                      for r in h.get("removed_lines", [])]
            from app.models.review import DiffHunk
            hunks.append(DiffHunk(
                old_start=h.get("old_start", 0),
                old_length=h.get("old_length", 0),
                new_start=h.get("new_start", 0),
                new_length=h.get("new_length", 0),
                added_lines=added,
                removed_lines=removed,
            ))
        changed_files.append(ChangedFile(
            file_path=f.get("file_path", ""),
            change_type=f.get("change_type", "modified"),
            additions=f.get("additions", 0),
            deletions=f.get("deletions", 0),
            hunks=hunks,
        ))

    issues: list[ReviewIssue] = []
    for i in state.get("review_issues") or []:
        issues.append(ReviewIssue(
            file_path=i.get("file_path", ""),
            line_no=i.get("line_no"),
            severity=i.get("severity", "low"),
            category=i.get("category", "maintainability"),
            title=i.get("title", ""),
            description=i.get("description", ""),
            suggestion=i.get("suggestion", ""),
            confidence=i.get("confidence", 0.5),
        ))

    return ReviewTask(
        id=state.get("task_id", ""),
        status="completed",
        pr_url=state.get("pr_url", ""),
        model=state.get("model"),
        pr=pr,
        changed_files=changed_files,
        issues=issues,
        report_markdown=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _rebuild_ref(data: dict):
    from app.models.review import PullRequestRef
    return PullRequestRef(
        ref=data.get("ref", ""),
        sha=data.get("sha", ""),
        repo_clone_url=data.get("repo_clone_url", ""),
    )
