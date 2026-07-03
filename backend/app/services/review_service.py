import asyncio
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.agents.providers import LLMProvider
from app.graph.builder import build_review_graph
from app.graph.state import ReviewState
from app.models.review import (
    ReviewCreateRequest,
    ReviewTask,
    StepStatus,
    TaskStatus,
    TaskStep,
)
from app.services.report_service import ReportService
from app.services.review_rebuild import rebuild_task_from_state
from app.tools.diff_parser import DiffParser
from app.tools.git_tool import GitTool
from app.tools.github_tool import GitHubTool

logger = logging.getLogger("RepoGuardian.Service")


class ReviewService:
    """审查服务：协调从任务创建到图执行的完整生命周期。

    流程概览：
        create_task()          → 创建 ReviewTask，存入内存，后台启动图
        _run_graph()           → 构建 StateGraph，注入工具，执行 ainvoke
        _sync_result_to_task() → 将图状态字典重建为 Pydantic 模型写回任务
    """

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
        """创建审查任务并异步启动图执行。"""
        task = ReviewTask(
            id=uuid4().hex,
            status=TaskStatus.pending,
            pr_url=str(request.pr_url),
            model=request.model,
            steps=[TaskStep(name="queued", message="任务已创建")],
        )
        self._tasks[task.id] = task
        logger.info("📋 创建审查任务 %s（PR: %s, 模型: %s）", task.id[:8], request.pr_url, request.model or "默认")
        asyncio.create_task(self._run_graph(task.id))
        return task

    def get_task(self, task_id: str) -> ReviewTask | None:
        return self._tasks.get(task_id)

    async def _run_graph(self, task_id: str) -> None:
        """执行 LangGraph 审查流程的核心方法。

        1. 构建初始状态字典（ReviewState），注入所有工具实例
        2. 编译并执行 StateGraph
        3. 成功 → 将结果同步到 ReviewTask
        4. 失败 → 标记任务状态为 failed
        5. 始终清理临时克隆仓库
        """
        task = self._tasks[task_id]
        task.status = TaskStatus.running
        self._touch(task)

        logger.info("🚀 开始执行审查图，任务 %s", task_id[:8])

        initial_state: ReviewState = {
            "task_id": task_id,
            "mode": "pr_review",
            "status": "running",
            "pr_url": task.pr_url,
            "model": task.model,
            "fix_iteration": 0,
            "max_fix_iterations": 3,
            "agent_loop_count": 0,
            "max_agent_loops": 6,
            "invalid_action_count": 0,
            "agent_events": [],
            "_github_tool": self._github_tool,
            "_git_tool": self._git_tool,
            "_diff_parser": self._diff_parser,
            "_provider": self._provider,
        }

        result = None
        try:
            graph = build_review_graph(phase=2)
            compiled = graph.compile()
            logger.info("📊 开始 ainvoke 执行...")
            result = await compiled.ainvoke(initial_state)
            logger.info("✅ ainvoke 执行完成，开始同步结果")
            self._sync_result_to_task(task, result)
            logger.info("🎉 审查任务 %s 完成", task_id[:8])
        except Exception as exc:
            logger.error("❌ 审查任务 %s 执行失败: %s", task_id[:8], exc)
            task.status = TaskStatus.failed
            task.error = str(exc)
            self._touch(task)
        finally:
            if result and result.get("repo_path"):
                _cleanup_repo(Path(result["repo_path"]))

    def _sync_result_to_task(self, task: ReviewTask, result: dict) -> None:
        """将图的扁平字典状态重建为 Pydantic 模型并写回 ReviewTask。"""
        rebuilt = rebuild_task_from_state(result)
        task.status = TaskStatus.completed
        task.pr = rebuilt.pr
        task.changed_files = rebuilt.changed_files
        task.issues = rebuilt.issues
        task.context_snippets = rebuilt.context_snippets
        task.repo_snapshot = rebuilt.repo_snapshot
        task.static_results = rebuilt.static_results
        task.patches = rebuilt.patches
        task.test_results = rebuilt.test_results
        task.agent_events = rebuilt.agent_events
        task.report_markdown = rebuilt.report_markdown
        task.steps = [
            TaskStep(
                name=step.get("node", f"step_{index}"),
                status=StepStatus.completed,
                message=step.get("message", ""),
            )
            for index, step in enumerate(result.get("step_progress") or [], start=1)
        ]
        self._touch(task)

    def _touch(self, task: ReviewTask) -> None:
        task.updated_at = datetime.now(timezone.utc)


def _cleanup_repo(repo_path: Path) -> None:
    """清理克隆的临时仓库目录。"""
    logger.info("🧹 清理临时仓库: %s", repo_path)
    try:
        shutil.rmtree(repo_path, ignore_errors=True)
    except Exception:
        pass
