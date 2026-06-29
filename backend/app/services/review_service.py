import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from app.agents.providers import LLMProvider
from app.models.review import ReviewCreateRequest, ReviewTask, StepStatus, TaskStatus, TaskStep
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
                TaskStep(name="解析 PR URL"),
                TaskStep(name="获取 PR 信息"),
                TaskStep(name="克隆仓库并生成 diff"),
                TaskStep(name="解析 diff"),
                TaskStep(name="LLM 代码审查"),
                TaskStep(name="生成 Markdown 报告"),
            ],
        )
        self._tasks[task.id] = task
        asyncio.create_task(self._run_task(task.id))
        return task

    def get_task(self, task_id: str) -> ReviewTask | None:
        return self._tasks.get(task_id)

    async def _run_task(self, task_id: str) -> None:
        task = self._tasks[task_id]
        task.status = TaskStatus.running
        self._touch(task)
        try:
            self._complete_step(task, 0, "PR URL 已接收")

            self._start_step(task, 1)
            task.pr = await self._github_tool.fetch_pr(task.pr_url)
            self._complete_step(task, 1, f"已获取 {task.pr.owner}/{task.pr.repo}#{task.pr.number}")

            self._start_step(task, 2)
            _, diff_text = await asyncio.to_thread(self._git_tool.clone_and_diff, task.pr)
            self._complete_step(task, 2, "diff 已生成")

            self._start_step(task, 3)
            task.changed_files = self._diff_parser.parse(diff_text)
            self._complete_step(task, 3, f"解析到 {len(task.changed_files)} 个变更文件")

            self._start_step(task, 4)
            if task.changed_files:
                task.issues = await self._provider.review(
                    task.pr,
                    task.changed_files,
                    diff_text,
                    task.model,
                )
                self._complete_step(task, 4, f"发现 {len(task.issues)} 个问题")
            else:
                task.issues = []
                self._complete_step(task, 4, "未解析到变更文件，跳过 LLM 审查")

            self._start_step(task, 5)
            task.report_markdown = self._report_service.generate(task)
            self._complete_step(task, 5, "报告已生成")
            task.status = TaskStatus.completed
        except Exception as exc:
            task.status = TaskStatus.failed
            task.error = str(exc)
            self._fail_running_step(task, task.error)
        finally:
            self._touch(task)

    def _start_step(self, task: ReviewTask, index: int) -> None:
        step = task.steps[index]
        step.status = StepStatus.running
        step.started_at = datetime.now(timezone.utc)
        self._touch(task)

    def _complete_step(self, task: ReviewTask, index: int, message: str) -> None:
        step = task.steps[index]
        if step.started_at is None:
            step.started_at = datetime.now(timezone.utc)
        step.status = StepStatus.completed
        step.message = message
        step.finished_at = datetime.now(timezone.utc)
        self._touch(task)

    def _fail_running_step(self, task: ReviewTask, message: str) -> None:
        for step in task.steps:
            if step.status == StepStatus.running:
                step.status = StepStatus.failed
                step.message = message
                step.finished_at = datetime.now(timezone.utc)
                return

    @staticmethod
    def _touch(task: ReviewTask) -> None:
        task.updated_at = datetime.now(timezone.utc)