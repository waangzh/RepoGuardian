import asyncio
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.agents.providers import LLMProvider
from app.graph.builder import build_review_graph
from app.graph.state import ReviewState
from app.models.review import (
    ChangedFile,
    ChangedLine,
    ContextSnippet,
    DiffHunk,
    PullRequestInfo,
    PullRequestRef,
    RepoSnapshot,
    ReviewCreateRequest,
    ReviewIssue,
    ReviewTask,
    StepStatus,
    TaskStatus,
    TaskStep,
)
from app.services.report_service import ReportService
from app.tools.diff_parser import DiffParser
from app.tools.git_tool import GitTool
from app.tools.github_tool import GitHubTool


class ReviewService:
    def __init__(
        self,
        github_tool: GitHubTool,
        git_tool: GitTool,
        diff_parser: DiffParser,
        provider: LLMProvider,
        report_service: ReportService,
    ) -> None:
        self._github_tool = github_tool
        self._git_tool = git_tool
        self._diff_parser = diff_parser
        self._provider = provider
        self._report_service = report_service
        self._tasks: dict[str, ReviewTask] = {}

    def create_task(self, request: ReviewCreateRequest) -> ReviewTask:
        task = ReviewTask(
            id=uuid4().hex,
            status=TaskStatus.pending,
            pr_url=str(request.pr_url),
            model=request.model,
            steps=[
                TaskStep(name="解析输入"),
                TaskStep(name="准备仓库"),
                TaskStep(name="解析 diff"),
                TaskStep(name="建立索引"),
                TaskStep(name="检索上下文"),
                TaskStep(name="LLM 代码审查"),
                TaskStep(name="生成 Markdown 报告"),
            ],
        )
        self._tasks[task.id] = task
        asyncio.create_task(self._run_graph(task.id))
        return task

    def get_task(self, task_id: str) -> ReviewTask | None:
        return self._tasks.get(task_id)

    async def _run_graph(self, task_id: str) -> None:
        task = self._tasks[task_id]
        task.status = TaskStatus.running
        self._touch(task)

        initial_state: ReviewState = {
            "task_id": task_id,
            "mode": "pr_review",
            "status": "running",
            "pr_url": task.pr_url,
            "model": task.model,
            "fix_iteration": 0,
            "max_fix_iterations": 3,
            "_github_tool": self._github_tool,
            "_git_tool": self._git_tool,
            "_diff_parser": self._diff_parser,
            "_provider": self._provider,
        }

        result = None
        try:
            graph = build_review_graph(phase=2)
            compiled = graph.compile()
            result = await compiled.ainvoke(initial_state)
            self._sync_result_to_task(task, result)
        except Exception as exc:
            task.status = TaskStatus.failed
            task.error = str(exc)
            self._touch(task)
        finally:
            if result and result.get("repo_path"):
                _cleanup_repo(Path(result["repo_path"]))

    def _sync_result_to_task(self, task: ReviewTask, result: dict) -> None:
        task.status = TaskStatus.completed

        pr_info_dict = result.get("pr_info") or {}
        task.pr = _rebuild_pr_info(pr_info_dict)

        task.changed_files = _rebuild_changed_files(result.get("changed_files") or [])
        task.issues = _rebuild_issues(result.get("review_issues") or [])
        task.context_snippets = _rebuild_context_snippets(result.get("context_snippets") or [])
        task.repo_snapshot = _rebuild_repo_snapshot(result.get("project_meta") or {})
        task.report_markdown = result.get("report_markdown")

        step_progress = result.get("step_progress") or []
        for i, step in enumerate(task.steps):
            if i < len(step_progress):
                step.status = StepStatus.completed
                step.message = step_progress[i].get("message", "")
        self._touch(task)

    def _touch(self, task: ReviewTask) -> None:
        task.updated_at = datetime.now(timezone.utc)


def _cleanup_repo(repo_path: Path) -> None:
    try:
        shutil.rmtree(repo_path, ignore_errors=True)
    except Exception:
        pass


def _rebuild_pr_info(data: dict) -> PullRequestInfo:
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


def _rebuild_changed_files(data: list[dict]) -> list[ChangedFile]:
    result: list[ChangedFile] = []
    for f in data:
        hunks = []
        for h in f.get("hunks", []):
            added = [ChangedLine(line_no=a.get("line_no"), content=a.get("content", ""))
                     for a in h.get("added_lines", [])]
            removed = [ChangedLine(line_no=r.get("line_no"), content=r.get("content", ""))
                      for r in h.get("removed_lines", [])]
            hunks.append(DiffHunk(
                old_start=h.get("old_start", 0),
                old_length=h.get("old_length", 0),
                new_start=h.get("new_start", 0),
                new_length=h.get("new_length", 0),
                added_lines=added,
                removed_lines=removed,
            ))
        result.append(ChangedFile(
            file_path=f.get("file_path", ""),
            change_type=f.get("change_type", "modified"),
            additions=f.get("additions", 0),
            deletions=f.get("deletions", 0),
            hunks=hunks,
        ))
    return result


def _rebuild_context_snippets(data: list[dict]) -> list[ContextSnippet]:
    return [
        ContextSnippet(
            file=i.get("file", ""),
            start_line=i.get("start_line", 1),
            end_line=i.get("end_line", i.get("start_line", 1)),
            content=i.get("content", ""),
            relevance=i.get("relevance", "adjacent"),
            symbol=i.get("symbol"),
        )
        for i in data
    ]


def _rebuild_repo_snapshot(data: dict) -> RepoSnapshot | None:
    if not data:
        return None
    return RepoSnapshot(
        language=data.get("language", "unknown"),
        framework=data.get("framework"),
        test_framework=data.get("test_framework"),
        total_files=data.get("total_files", 0),
    )


def _rebuild_issues(data: list[dict]) -> list[ReviewIssue]:
    return [
        ReviewIssue(
            file_path=i.get("file_path", ""),
            line_no=i.get("line_no"),
            severity=i.get("severity", "low"),
            category=i.get("category", "maintainability"),
            title=i.get("title", ""),
            description=i.get("description", ""),
            suggestion=i.get("suggestion", ""),
            confidence=i.get("confidence", 0.5),
        )
        for i in data
    ]
