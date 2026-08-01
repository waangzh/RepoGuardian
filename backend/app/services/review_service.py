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
from app.graph.nodes.issue_validation import issue_policy_node, issue_verifier_node
from app.graph.nodes.resolve_evidence import resolve_evidence_node
from app.graph.state import ReviewState
from app.models.review import (
    ExecutionBudget,
    IssueMetrics,
    PatchStatus,
    PatchValidationResult,
    ReviewCreateRequest,
    ReviewMode,
    ReviewIssue,
    RepositorySnapshot,
    ReviewPreviewRequest,
    ReviewPreviewResponse,
    ValidationBackendPreview,
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
from app.services.issue_deduplication import IssueDeduplicationService
from app.services.review_rebuild import rebuild_task_from_state
from app.services.review_planner import DeterministicReviewPlanner
from app.services.review_unit_executor import ReviewUnitExecutor
from app.services.patch_presentation import build_patch_presentation
from app.tools.diff_parser import DiffParser
from app.tools.git_tool import GitTool
from app.tools.github_tool import GitHubTool
from app.tools.repo_indexer import RepoIndexer
from app.tools.command_runner import CommandExecutor
from app.validation.selector import ValidationBackendSelector

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
        validation_backend_selector: ValidationBackendSelector | None = None,
    ) -> None:
        self._github_tool = github_tool
        self._git_tool = git_tool
        self._diff_parser = diff_parser
        self._provider = provider
        self._report_service = report_service
        # 兼容旧构造签名，但阶段 5A 不把命令执行器注入验证路径。
        self._command_executor = command_executor
        self._validation_backend_selector = validation_backend_selector or ValidationBackendSelector()
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
            validation_profile=request.validation_profile,
            review={"mode": request.mode, "status": TaskStatus.queued, "completed": False},
            validation=[],
            steps=[TaskStep(name="queued", message="任务已创建")],
        )
        self._tasks[task.id] = task
        logger.info("📋 创建审查任务 %s（PR: %s, 模型: %s）", task.id[:8], request.pr_url, request.model or "默认")
        self._run_tasks[task.id] = asyncio.create_task(self._run_graph(task.id))
        return task

    def get_task(self, task_id: str) -> ReviewTask | None:
        return self._tasks.get(task_id)

    def apply_user_runner_result(
        self,
        task_id: str,
        patch_id: str,
        result: PatchValidationResult,
    ) -> None:
        """把已由 UserRunnerService 验真的结果原子地回写到内存任务。"""
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.status == TaskStatus.cancelled:
            raise ValueError("cancelled review cannot accept validation results")
        patch = next((item for item in task.patches if item.id == patch_id), None)
        if patch is None:
            raise KeyError(patch_id)
        if (
            result.backend != ValidationBackend.user_runner.value
            or not result.trusted
            or result.trust_source != "user_runner"
            or result.head_sha != patch.head_sha
            or result.patch_sha != patch.patch_sha
        ):
            raise ValueError("runner result does not match the candidate patch")

        if result.status == ValidationStatus.passed:
            patch.status = PatchStatus.verified
        elif result.status == ValidationStatus.failed:
            patch.status = PatchStatus.validation_failed
        else:
            patch.status = PatchStatus.validation_inconclusive
        patch.validation_backend = result.backend
        patch.validation_result_id = result.id
        patch.presentation = build_patch_presentation(patch)

        validation = ValidationResult.model_validate({
            **result.model_dump(mode="json"),
            "patch_id": patch.id,
        })
        task.validation = [
            item for item in task.validation
            if not (
                item.patch_id == patch.id
                and item.backend == ValidationBackend.user_runner.value
            )
        ]
        task.validation.append(validation)
        self._touch(task)

    def apply_project_ci_result(
        self,
        task_id: str,
        patch_id: str,
        result: PatchValidationResult,
    ) -> None:
        """仅接受完成了全部 CI 身份绑定校验的结构化结果。"""
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        patch = next((item for item in task.patches if item.id == patch_id), None)
        if patch is None:
            raise KeyError(patch_id)
        if (
            result.backend != ValidationBackend.project_ci.value
            or result.head_sha != patch.head_sha
            or result.patch_sha != patch.patch_sha
        ):
            raise ValueError("project CI result does not match the candidate patch")
        if result.status in {ValidationStatus.passed, ValidationStatus.failed} and (
            not result.trusted
            or not (result.trust_source or "").startswith("project_ci:")
        ):
            raise ValueError("untrusted project CI result cannot decide candidate correctness")
        if not (result.trust_source or "").startswith("project_ci"):
            raise ValueError("project CI trust source is missing")

        if result.status == ValidationStatus.passed:
            patch.status = PatchStatus.verified
        elif result.status == ValidationStatus.failed:
            patch.status = PatchStatus.validation_failed
        else:
            patch.status = PatchStatus.validation_inconclusive
        patch.validation_backend = result.backend
        patch.validation_result_id = result.id
        patch.presentation = build_patch_presentation(patch)

        validation = ValidationResult.model_validate({
            **result.model_dump(mode="json"),
            "patch_id": patch.id,
        })
        task.validation = [
            item for item in task.validation
            if not (
                item.patch_id == patch.id
                and item.backend == ValidationBackend.project_ci.value
            )
        ]
        task.validation.append(validation)
        self._touch(task)

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
            backend = self._validation_backend_selector.select(
                request.validation_backend.value,
                request.mode,
            )
            capabilities = await backend.capabilities(RepositorySnapshot(
                language=index.get("project_meta", {}).get("language", "unknown"),
                framework=index.get("project_meta", {}).get("framework"),
                test_framework=index.get("project_meta", {}).get("test_framework"),
                total_files=len(index["file_index"]),
            ))
            return ReviewPreviewResponse(
                mode=request.mode,
                changed_file_count=len(plan.changed_files),
                included_file_count=sum(item.included for item in plan.changed_files),
                changed_files=plan.changed_files,
                review_units=plan.review_units,
                excluded_files=plan.excluded_files,
                matched_rules=plan.matched_rules,
                risk_tags=plan.risk_tags,
                estimated_model_calls=sum(
                    planner.estimated_model_calls(unit) for unit in plan.review_units
                ),
                estimated_tokens=sum(unit.estimated_tokens for unit in plan.review_units),
                patch_generation_enabled=(
                    request.mode != ReviewMode.review and request.generate_patches
                ),
                validation_backend=ValidationBackendPreview(
                    name=ValidationBackend(backend.name),
                    available=capabilities.available,
                    unavailable_reason=capabilities.unavailable_reason,
                ),
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
            "validation_profile": task.validation_profile,
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
            "_command_executor": None,
            "_validation_backend_selector": self._validation_backend_selector,
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
        task.validation_profile = rebuilt.validation_profile
        task.review = rebuilt.review
        task.pr = rebuilt.pr
        task.changed_files = rebuilt.changed_files
        task.review_units = rebuilt.review_units
        task.review_unit_results = rebuilt.review_unit_results
        task.excluded_files = rebuilt.excluded_files
        task.issues = rebuilt.issues
        task.issue_metrics = rebuilt.issue_metrics
        task.context_snippets = rebuilt.context_snippets
        task.repo_snapshot = rebuilt.repo_snapshot
        task.project_profile = rebuilt.project_profile
        task.static_results = rebuilt.static_results
        task.validation_snapshots = rebuilt.validation_snapshots
        task.validation_deltas = rebuilt.validation_deltas
        task.validation = rebuilt.validation
        task.patch_eligibility = rebuilt.patch_eligibility
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
                aggregated_issues = list(task.issues)
                retry_metrics = task.issue_metrics
                lifecycle_warnings: list[str] = []
                if result.status == ReviewUnitStatus.completed:
                    lifecycle_state: ReviewState = {
                        **state,
                        "base_sha": pr.base.sha,
                        "review_units": [item.model_dump(mode="json") for item in task.review_units],
                        "review_unit_results": [result.model_dump(mode="json")],
                        "review_issues": [item.model_dump(mode="json") for item in result.issues],
                        "context_snippets": [
                            item.model_dump(mode="json") for item in result.context_snippets
                        ],
                        "warnings": [],
                        "_git_tool": self._git_tool,
                        "_provider": self._provider,
                    }
                    resolved = await resolve_evidence_node(lifecycle_state)
                    lifecycle_state = ReviewState(**{**lifecycle_state, **resolved})
                    checked = await issue_policy_node(lifecycle_state)
                    lifecycle_state = ReviewState(**{**lifecycle_state, **checked})
                    verified = await issue_verifier_node(lifecycle_state)
                    lifecycle_state = ReviewState(**{**lifecycle_state, **verified})
                    verified_new = [
                        item for item in lifecycle_state.get("review_issues") or []
                    ]
                    existing = [
                        item for item in task.issues if item.review_unit_id != unit_id
                    ]
                    retry_metrics = IssueMetrics.model_validate(
                        lifecycle_state.get("issue_metrics") or {}
                    ).model_copy(update={
                        "candidate_issue_count": max(
                            len(result.issues), task.issue_metrics.candidate_issue_count
                        ),
                    })
                    deduplicated = await IssueDeduplicationService().aggregate(
                        [
                            *existing,
                            *(ReviewIssue.model_validate(item) for item in verified_new),
                        ],
                        self._provider,
                        task.model,
                        retry_metrics,
                    )
                    aggregated_issues = deduplicated.issues
                    retry_metrics = deduplicated.metrics
                    lifecycle_warnings = list(lifecycle_state.get("warnings") or [])
                    result = result.model_copy(update={
                        "issues": [
                            issue for issue in aggregated_issues
                            if issue.review_unit_id == unit_id
                        ],
                    })
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
            task.issues = aggregated_issues
            task.issue_metrics = retry_metrics
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
            task.warnings = list(dict.fromkeys([*task.warnings, *lifecycle_warnings]))
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
