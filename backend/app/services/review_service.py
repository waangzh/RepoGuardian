import asyncio
import logging
import shutil
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.tracers.langchain import LangChainTracer
from langsmith import Client, tracing_context

from app.agents.providers import LLMProvider
from app.core.config import settings
from app.graph.builder import build_review_graph
from app.graph.state import ReviewState
from app.models.review import (
    ExecutionBudget,
    ReviewCreateRequest,
    ReviewMode,
    ReviewPreviewRequest,
    ReviewPreviewResponse,
    ReviewUnitResult,
    ReviewUnitStatus,
    ReviewPhase,
    ReviewTask,
    StepStatus,
    TaskStatus,
    TaskStep,
    ValidationBackend,
    ValidationResult,
    ValidationStatus,
)
from app.services.report_service import ReportService
from app.services.review_rebuild import rebuild_task_from_state
from app.services.review_planner import DeterministicReviewPlanner
from app.services.review_unit_executor import ReviewUnitExecutor
from app.tools.diff_parser import DiffParser
from app.tools.git_tool import GitTool
from app.tools.github_tool import GitHubTool
from app.tools.repo_indexer import RepoIndexer
from app.tools.command_runner import CommandExecutor, build_command_executor

logger = logging.getLogger("RepoGuardian.Service")

_TRACE_REDACTED_KEYS = {
    "api_key",
    "authorization",
    "base_url",
    "clone_url",
    "github_token",
    "headers",
    "html_url",
    "langsmith_api_key",
    "openai_api_key",
    "pr_url",
    "repo_clone_url",
    "repo_path",
}


class ReviewService:
    """
    审查服务：协调从任务创建到图执行的完整生命周期。
    流程：
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
        command_executor: CommandExecutor | None = None,
    ) -> None:
        self._github_tool = github_tool
        self._git_tool = git_tool
        self._diff_parser = diff_parser
        self._provider = provider
        self._report_service = report_service
        # 只读审查不构造也不使用执行器；仅显式验证模式才会注入它。
        self._command_executor = command_executor
        self._tasks: dict[str, ReviewTask] = {}
        self._run_tasks: dict[str, asyncio.Task[None]] = {}
        self._retry_locks: dict[str, asyncio.Lock] = {}
        self._repo_paths: dict[str, Path] = {}

    def create_task(self, request: ReviewCreateRequest) -> ReviewTask:
        """创建审查任务并异步启动图执行。"""
        task = ReviewTask(
            id=uuid4().hex,
            status=TaskStatus.queued,
            pr_url=str(request.pr_url),
            model=request.model,
            mode=request.mode,
            generate_patches=request.generate_patches,
            validation_backend=request.validation_backend,
            review={"mode": request.mode, "status": TaskStatus.queued, "completed": False},
            validation=[ValidationResult(
                backend=request.validation_backend,
                status=(
                    ValidationStatus.not_requested
                    if request.mode != ReviewMode.review_suggest_and_validate
                    else ValidationStatus.queued
                ),
            )],
            steps=[TaskStep(name="queued", message="任务已创建")],
        )
        self._tasks[task.id] = task
        logger.info("📋 创建审查任务 %s（PR: %s, 模型: %s）", task.id[:8], request.pr_url, request.model or "默认")
        self._run_tasks[task.id] = asyncio.create_task(self._run_graph(task.id))
        return task

    def get_task(self, task_id: str) -> ReviewTask | None:
        return self._tasks.get(task_id)

    async def preview(self, request: ReviewPreviewRequest) -> ReviewPreviewResponse:
        """只执行 PR 获取、diff 解析和确定性规划。"""
        pr_url = str(request.pr_url)
        pr = await self._github_tool.fetch_pr(pr_url)
        # Preview 与正式执行共享 clone + 只读索引输入，保证 related_files 和
        # fingerprint 一致；这一过程不运行仓库代码。
        repo_path, diff_text = await asyncio.to_thread(self._git_tool.clone_and_diff, pr)
        try:
            changed_files = self._diff_parser.parse(diff_text)
            index = await RepoIndexer().execute(repo_path=str(repo_path))
            planner = DeterministicReviewPlanner()
            plan = planner.plan(
                changed_files,
                base_sha=pr.base.sha,
                head_sha=pr.head.sha,
                file_index=index["file_index"],
                symbol_index=index["symbol_index"],
            )
            return ReviewPreviewResponse(
                changed_files=plan.changed_files,
                review_units=plan.review_units,
                excluded_files=plan.excluded_files,
                matched_rules=plan.matched_rules,
                risk_tags=plan.risk_tags,
                estimated_model_calls=sum(
                    planner.estimated_model_calls(unit) for unit in plan.review_units
                ),
                estimated_tokens=sum(unit.estimated_tokens for unit in plan.review_units),
                warnings=plan.warnings,
            )
        finally:
            _cleanup_repo(Path(repo_path))

    async def _run_graph(self, task_id: str) -> None:
        """执行 LangGraph 审查流程的核心方法。

        1. 构建初始状态字典（ReviewState），注入所有工具实例
        2. 编译并执行 StateGraph
        3. 成功 → 将结果同步到 ReviewTask
        4. 失败 → 标记任务状态为 failed
        5. 始终清理临时克隆仓库
        """
        task = self._tasks[task_id]
        task.status = TaskStatus.planning
        task.review.status = task.status
        self._touch(task)

        logger.info("🚀 开始执行审查图，任务 %s", task_id[:8])

        initial_state: ReviewState = {
            "task_id": task_id,
            "mode": task.mode.value,
            "status": TaskStatus.planning.value,
            "generate_patches": task.generate_patches,
            "validation_backend": task.validation_backend.value,
            "validation_results": [item.model_dump(mode="json") for item in task.validation],
            "warnings": [],
            "pr_url": task.pr_url,
            "model": task.model,
            "execution_budget": ExecutionBudget().model_dump(),
            "agent_events": [],
            "_github_tool": self._github_tool,
            "_git_tool": self._git_tool,
            "_diff_parser": self._diff_parser,
            "_provider": self._provider,
            "_command_executor": (
                self._command_executor or build_command_executor()
                if task.mode == ReviewMode.review_suggest_and_validate
                and task.validation_backend == ValidationBackend.local
                else None
            ),
            "_repo_prepared_callback": lambda path: self._repo_paths.__setitem__(
                task_id, Path(path)
            ),
        }

        result = None
        try:
            graph = build_review_graph(phase=2)
            compiled = graph.compile()
            run_metadata = {
                "task_id": task_id,
                "mode": task.mode.value,
                "model_override": task.model is not None,
            }
            run_config: dict[str, Any] = {
                "run_name": "repoguardian-pr-review",
                "tags": ["repoguardian", "pr_review"],
                "metadata": run_metadata,
            }
            tracing, callbacks = _build_langsmith_tracing(run_metadata)
            if callbacks:
                run_config["callbacks"] = callbacks
            logger.info("📊 开始 ainvoke 执行...")
            with tracing:
                result = await compiled.ainvoke(initial_state, config=run_config)
            logger.info("✅ ainvoke 执行完成，开始同步结果")
            self._sync_result_to_task(task, result)
            logger.info("🎉 审查任务 %s 完成", task_id[:8])
        except asyncio.CancelledError:
            task.status = TaskStatus.cancelled
            task.error = None
            self._touch(task)
            raise
        except Exception as exc:
            logger.error("❌ 审查任务 %s 执行失败: %s", task_id[:8], exc)
            task.status = TaskStatus.failed
            task.phase = ReviewPhase.failed
            task.error = str(exc)
            self._touch(task)
        finally:
            repo_path = (
                Path(result["repo_path"])
                if result and result.get("repo_path")
                else self._repo_paths.get(task_id)
            )
            if repo_path is not None:
                _cleanup_repo(repo_path)
            self._repo_paths.pop(task_id, None)
            self._run_tasks.pop(task_id, None)

    def _sync_result_to_task(self, task: ReviewTask, result: dict) -> None:
        """将图的扁平字典状态重建为 Pydantic 模型并写回 ReviewTask。"""
        rebuilt = rebuild_task_from_state(result)
        task.status = rebuilt.status
        task.phase = rebuilt.phase
        task.mode = rebuilt.mode
        task.generate_patches = rebuilt.generate_patches
        task.validation_backend = rebuilt.validation_backend
        task.review = rebuilt.review
        task.pr = rebuilt.pr
        task.changed_files = rebuilt.changed_files
        task.review_units = rebuilt.review_units
        task.review_unit_results = rebuilt.review_unit_results
        task.excluded_files = rebuilt.excluded_files
        task.issues = rebuilt.issues
        task.context_snippets = rebuilt.context_snippets
        task.repo_snapshot = rebuilt.repo_snapshot
        task.project_profile = rebuilt.project_profile
        task.static_results = rebuilt.static_results
        task.validation_snapshots = rebuilt.validation_snapshots
        task.validation_deltas = rebuilt.validation_deltas
        task.validation = rebuilt.validation
        task.patches = rebuilt.patches
        task.test_results = rebuilt.test_results
        task.agent_events = rebuilt.agent_events
        task.human_request = rebuilt.human_request
        task.report_markdown = rebuilt.report_markdown
        task.warnings = rebuilt.warnings
        task.steps = [
            TaskStep(
                name=step.get("node", f"step_{index}"),
                status=StepStatus.completed,
                message=step.get("message", ""),
            )
            for index, step in enumerate(result.get("step_progress") or [], start=1)
        ]
        self._touch(task)

    def cancel_task(self, task_id: str) -> bool:
        """取消主任务；取消会沿 await 链传播到所有 Unit worker。"""
        run_task = self._run_tasks.get(task_id)
        if run_task is None or run_task.done():
            return False
        run_task.cancel()
        return True

    async def retry_unit(self, task_id: str, unit_id: str) -> ReviewUnitResult:
        """在新的临时 clone 中只重试一个 Unit，并原位替换其聚合结果。"""
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.status not in {
            TaskStatus.completed,
            TaskStatus.completed_with_warnings,
            TaskStatus.failed,
        }:
            raise ValueError("review task must be terminal before retrying a unit")
        unit = next((item for item in task.review_units if item.id == unit_id), None)
        if unit is None:
            raise KeyError(unit_id)

        lock = self._retry_locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            previous_status = task.status
            previous_phase = task.phase
            previous_completed = task.review.completed
            task.status = TaskStatus.reviewing
            task.review.status = task.status
            task.review.completed = False
            self._touch(task)
            try:
                pr = task.pr or await self._github_tool.fetch_pr(task.pr_url)
                repo_path, diff_text = await asyncio.to_thread(self._git_tool.clone_and_diff, pr)
            except BaseException:
                task.status = previous_status
                task.phase = previous_phase
                task.review.status = previous_status
                task.review.completed = previous_completed
                self._touch(task)
                raise
            try:
                changed_files = self._diff_parser.parse(diff_text)
                index = await RepoIndexer().execute(repo_path=str(repo_path))
                state = {
                    "task_id": task.id,
                    "model": task.model,
                    "pr_info": pr.model_dump(mode="json"),
                    "repo_path": str(repo_path),
                    "changed_files": [item.model_dump(mode="json") for item in changed_files],
                    "file_index": index["file_index"],
                    "symbol_index": index["symbol_index"],
                }
                result = await ReviewUnitExecutor(
                    self._provider,
                    concurrency=1,
                    timeout_seconds=settings.repoguardian_review_unit_timeout_seconds,
                ).execute_unit(unit, state)
            except BaseException:
                task.status = previous_status
                task.phase = previous_phase
                task.review.status = previous_status
                task.review.completed = previous_completed
                self._touch(task)
                raise
            finally:
                _cleanup_repo(Path(repo_path))

            previous = {item.review_unit_id: item for item in task.review_unit_results}
            previous[unit_id] = result
            task.review_unit_results = [
                previous[item.id] for item in task.review_units if item.id in previous
            ]
            task.issues = [item for item in task.issues if item.review_unit_id != unit_id]
            task.issues.extend(result.issues)
            task.context_snippets = [
                item for item in task.context_snippets if item.review_unit_id != unit_id
            ] + result.context_snippets
            task.agent_events = [
                item for item in task.agent_events if item.review_unit_id != unit_id
            ] + result.messages

            failed = [
                item for item in task.review_unit_results
                if item.status != ReviewUnitStatus.completed
            ]
            completed = len(task.review_unit_results) - len(failed)
            task.warnings = [
                warning for warning in task.warnings if "Review Unit" not in warning
            ]
            if failed and completed:
                task.status = TaskStatus.completed_with_warnings
                task.warnings.append(
                    f"{len(failed)} 个 Review Unit 失败，其他 {completed} 个 Unit 已完成"
                )
                task.error = None
            elif failed:
                task.status = TaskStatus.failed
                task.error = result.error or "all review units failed"
            else:
                task.status = (
                    TaskStatus.completed_with_warnings if task.warnings else TaskStatus.completed
                )
                task.error = None
            task.review.status = task.status
            task.review.completed = task.status in {
                TaskStatus.completed, TaskStatus.completed_with_warnings
            }
            task.phase = (
                ReviewPhase.failed if task.status == TaskStatus.failed else ReviewPhase.completed
            )
            task.report_markdown = self._report_service.generate(task)
            self._touch(task)
            return result

    def _touch(self, task: ReviewTask) -> None:
        task.updated_at = datetime.now(timezone.utc)


def _cleanup_repo(repo_path: Path) -> None:
    """清理克隆的临时仓库目录。"""
    logger.info("🧹 清理临时仓库: %s", repo_path)
    try:
        shutil.rmtree(repo_path, ignore_errors=True)
    except Exception:
        pass


def _build_langsmith_tracing(
    metadata: dict[str, Any],
) -> tuple[AbstractContextManager[None], list[LangChainTracer]]:
    """创建本次图调用专用的 LangSmith 配置，失败时无损降级。"""
    if not settings.repoguardian_langsmith_tracing or not settings.langsmith_api_key:
        return tracing_context(enabled=False), []

    try:
        client_options: dict[str, Any] = {
            "api_key": settings.langsmith_api_key,
            "hide_inputs": _trace_content_filter,
            "hide_outputs": _trace_content_filter,
        }
        if settings.langsmith_endpoint:
            client_options["api_url"] = settings.langsmith_endpoint
        client = Client(**client_options)
        tags = ["repoguardian", "pr_review"]
        tracer = LangChainTracer(
            project_name=settings.langsmith_project,
            client=client,
            tags=tags,
            metadata=metadata,
        )
        return (
            tracing_context(
                enabled=True,
                client=client,
                project_name=settings.langsmith_project,
                tags=tags,
                metadata=metadata,
            ),
            [tracer],
        )
    except Exception as exc:
        logger.warning("LangSmith 初始化失败，已跳过本次追踪: %s", type(exc).__name__)
        return tracing_context(enabled=False), []


def _trace_content_filter(value: dict[str, Any]) -> dict[str, Any]:
    """LangSmith 输入/输出过滤器：默认不上传内容，始终移除敏感字段。"""
    if not settings.repoguardian_langsmith_include_content:
        return {}
    filtered = _remove_sensitive_trace_values(value)
    return filtered if isinstance(filtered, dict) else {}


def _remove_sensitive_trace_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _remove_sensitive_trace_values(item)
            for key, item in value.items()
            if not key.startswith("_") and key.lower() not in _TRACE_REDACTED_KEYS
        }
    if isinstance(value, list):
        return [_remove_sensitive_trace_values(item) for item in value]
    if isinstance(value, tuple):
        return [_remove_sensitive_trace_values(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return "<redacted non-serializable value>"
