import asyncio
import logging
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.tracers.langchain import LangChainTracer
from langgraph.types import Command
from langsmith import Client, tracing_context

from app.agents.providers import LLMProvider
from app.core.config import settings
from app.graph.builder import build_review_graph
from app.graph.checkpointer import (
    delete_thread_checkpoints,
    get_checkpointer,
    review_thread_config,
)
from app.graph.nodes.issue_validation import issue_policy_node, issue_verifier_node
from app.graph.nodes.resolve_evidence import resolve_evidence_node
from app.graph.state import ReviewState
from app.models.review import (
    ExecutionBudget,
    HumanReviewRequest,
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
    TaskStepProgress,
    ValidationBackend,
    ValidationResult,
    ValidationStatus,
)
from app.services.report_service import ReportService
from app.services.issue_deduplication import IssueDeduplicationService
from app.services.review_rebuild import rebuild_task_from_state
from app.services.review_planner import DeterministicReviewPlanner
from app.services.review_unit_executor import ReviewUnitExecutor
from app.services.review_repository import ReviewRepository
from app.services.task_queue import ClaimedJob, DatabaseTaskQueue, ReviewWorker
from app.services.patch_presentation import build_patch_presentation
from app.tools.diff_parser import DiffParser
from app.tools.git_tool import GitTool
from app.tools.github_tool import GitHubTool
from app.tools.repo_indexer import RepoIndexer
from app.tools.workspace_cleanup import cleanup_workspace
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

_TASK_STATUS_BY_GRAPH_NODE = {
    "intake": TaskStatus.planning,
    "repo_prepare": TaskStatus.planning,
    "diff_parse": TaskStatus.planning,
    "repo_index": TaskStatus.planning,
    "project_detection": TaskStatus.planning,
    "review_plan": TaskStatus.reviewing,
    "review_units": TaskStatus.reviewing,
    "resolve_evidence": TaskStatus.resolving_evidence,
    "issue_policy": TaskStatus.verifying_issues,
    "issue_verifier": TaskStatus.verifying_issues,
    "issue_deduplication": TaskStatus.verifying_issues,
    "verification": TaskStatus.verifying_issues,
    "generate_patch": TaskStatus.generating_patches,
    "candidate_check": TaskStatus.generating_patches,
    "mark_unverified": TaskStatus.generating_patches,
    "validation": TaskStatus.validating,
    "repair_assessment": TaskStatus.validating,
}


class ReviewService:
    """
    审查服务：协调从任务创建到图执行的完整生命周期。
    流程：
        create_task()          → 创建 ReviewTask，存入内存，后台启动图
        _run_graph()           → 构建 StateGraph，注入工具，流式执行并同步进度
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
        repository: ReviewRepository | None = None,
        task_queue: DatabaseTaskQueue | None = None,
    ) -> None:
        self._github_tool = github_tool
        self._git_tool = git_tool
        self._diff_parser = diff_parser
        self._provider = provider
        self._report_service = report_service
        # 兼容旧构造签名，但阶段 5A 不把命令执行器注入验证路径。
        self._command_executor = command_executor
        self._validation_backend_selector = validation_backend_selector or ValidationBackendSelector()
        self._repository = repository
        self._task_queue = task_queue or (DatabaseTaskQueue() if repository else None)
        self._worker = ReviewWorker(self._task_queue, self._handle_job) if self._task_queue else None
        self._worker_task: asyncio.Task[None] | None = None
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
        if self._repository and self._task_queue:
            self._repository.create_task(task)
            self._task_queue.enqueue(task_id=task.id, idempotency_key=f"review:{task.id}:initial")
            self._ensure_worker_started()
        else:
            self._run_tasks[task.id] = asyncio.create_task(self._run_graph(task.id))
        return task

    def get_task(self, task_id: str) -> ReviewTask | None:
        if self._repository:
            return self._repository.get_task(task_id)
        return self._tasks.get(task_id)

    def list_tasks(self, *, status: str | None, page: int, page_size: int):
        if not self._repository:
            items = list(self._tasks.values())
            if status:
                items = [item for item in items if item.status.value == status]
            start = (page - 1) * page_size
            from app.models.persistence import ReviewTaskListResponse
            return ReviewTaskListResponse(
                items=items[start:start + page_size], total=len(items), page=page, page_size=page_size
            )
        return self._repository.list_tasks(status=status, page=page, page_size=page_size)

    def get_unit_detail(self, task_id: str, unit_id: str):
        return self._repository.get_unit(task_id, unit_id) if self._repository else None

    def get_issue_detail(self, task_id: str, issue_id: str):
        return self._repository.get_issue(task_id, issue_id) if self._repository else None

    def get_patch_detail(self, task_id: str, patch_id: str):
        return self._repository.get_patch(task_id, patch_id) if self._repository else None

    def get_validation_detail(self, task_id: str, validation_id: str):
        return self._repository.get_validation(task_id, validation_id) if self._repository else None

    def list_human_request_details(self, task_id: str):
        return self._repository.list_human_requests(task_id) if self._repository else []

    def get_human_request_detail(self, task_id: str, request_id: str):
        return (
            self._repository.get_human_request(task_id, request_id)
            if self._repository else None
        )

    def apply_user_runner_result(
        self,
        task_id: str,
        patch_id: str,
        result: PatchValidationResult,
    ) -> None:
        """把已由 UserRunnerService 验真的结果原子地回写到内存任务。"""
        task = self.get_task(task_id)
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
        self._persist(task)

    def apply_project_ci_result(
        self,
        task_id: str,
        patch_id: str,
        result: PatchValidationResult,
    ) -> None:
        """仅接受完成了全部 CI 身份绑定校验的结构化结果。"""
        task = self.get_task(task_id)
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
        self._persist(task)

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
                model=getattr(request, "model", None) or settings.repoguardian_model,
                provider=settings.repoguardian_provider,
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
            await _cleanup_repo(Path(repo_path))

    async def _handle_job(self, job: ClaimedJob) -> None:
        if job.kind == "review":
            await self._run_graph(job.task_id, resume=job.payload.get("resume"))
            return
        if job.kind == "unit_retry":
            await self.retry_unit(job.task_id, str(job.payload["unit_id"]))
            return
        raise ValueError(f"unsupported worker job kind: {job.kind}")

    def _ensure_worker_started(self) -> None:
        if self._worker is not None and (self._worker_task is None or self._worker_task.done()):
            self._worker_task = asyncio.create_task(self._worker.run_forever())

    async def _run_graph(self, task_id: str, *, resume: dict[str, Any] | None = None) -> None:
        """执行 LangGraph 审查流程的核心方法。

        1. 构建初始状态字典（ReviewState），注入所有工具实例
        2. 编译并执行 StateGraph
        3. 成功 → 将结果同步到 ReviewTask
        4. 失败 → 标记任务状态为 failed
        5. 始终清理临时克隆仓库
        """
        task = self.get_task(task_id) or self._tasks[task_id]
        if task.status == TaskStatus.cancelled:
            return
        task.status = TaskStatus.planning
        task.review.status = task.status
        self._touch(task)
        self._persist(task)

        logger.info("🚀 开始执行审查图，任务 %s", task_id[:8])

        initial_state: ReviewState = {
            "task_id": task_id,
            "mode": task.mode.value,
            "status": TaskStatus.planning.value,
            "created_at": task.created_at.isoformat(),
            "updated_at": task.updated_at.isoformat(),
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
        if self._repository:
            for key in (
                "_github_tool", "_git_tool", "_diff_parser", "_provider",
                "_command_executor", "_validation_backend_selector",
                "_repo_prepared_callback",
            ):
                initial_state.pop(key, None)
            initial_state["_human_interrupt_enabled"] = True
            initial_state["_report_purpose_model_enabled"] = True

        result = None
        try:
            graph = build_review_graph(phase=2)
            checkpointer = await get_checkpointer() if self._repository else None
            compiled = (
                graph.compile(checkpointer=checkpointer)
                if self._repository
                else graph.compile()
            )
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
            if self._repository:
                run_config.update(review_thread_config(task_id))
            tracing, callbacks = _build_langsmith_tracing(run_metadata)
            if callbacks:
                run_config["callbacks"] = callbacks
            logger.info("📊 开始流式执行审查图...")
            with tracing:
                graph_input: Any = Command(resume=resume) if resume is not None else initial_state
                result = await self._invoke_graph_with_progress(
                    compiled, graph_input, run_config, task
                )
            interrupt_payload = _extract_interrupt_payload(result)
            if interrupt_payload and self._repository:
                request = HumanReviewRequest.model_validate(interrupt_payload["request"])
                self._sync_result_to_task(task, result)
                checkpoint_tuple = await checkpointer.aget_tuple(run_config) if checkpointer else None
                checkpoint_id = (
                    checkpoint_tuple.config.get("configurable", {}).get("checkpoint_id")
                    if checkpoint_tuple else None
                )
                detail = self._repository.create_human_request(
                    task_id=task_id,
                    request=request,
                    reason=str(interrupt_payload.get("reason") or "human input required"),
                    request_id=str(interrupt_payload["request_id"]),
                    checkpoint_id=checkpoint_id,
                )
                task.status = TaskStatus.waiting_for_human
                task.review.status = task.status
                task.human_request = request
                self._touch(task)
                self._repository.save_task(task, checkpoint_id=checkpoint_id)
                self._repository.save_issue_lifecycle(task_id, result)
                logger.info("任务 %s 已暂停，等待人工请求 %s", task_id[:8], detail.request_id)
                return
            logger.info("✅ ainvoke 执行完成，开始同步结果")
            self._sync_result_to_task(task, result)
            self._persist(task)
            if self._repository:
                self._repository.save_issue_lifecycle(task_id, result)
            logger.info("🎉 审查任务 %s 完成", task_id[:8])
        except asyncio.CancelledError:
            task.status = TaskStatus.cancelled
            task.error = None
            self._touch(task)
            self._persist(task)
            raise
        except Exception as exc:
            logger.error("❌ 审查任务 %s 执行失败: %s", task_id[:8], exc)
            task.status = TaskStatus.failed
            task.phase = ReviewPhase.failed
            task.error = str(exc)
            self._touch(task)
            self._persist(task)
            if self._repository:
                raise
        finally:
            repo_path = (
                Path(result["repo_path"])
                if result and result.get("repo_path")
                else self._repo_paths.get(task_id)
            )
            if repo_path is not None and task.status != TaskStatus.waiting_for_human:
                await _cleanup_repo(repo_path)
            if self._repository and task.status in {
                TaskStatus.completed,
                TaskStatus.completed_with_warnings,
                TaskStatus.failed,
                TaskStatus.cancelled,
            }:
                try:
                    await delete_thread_checkpoints(task_id)
                except Exception:
                    logger.warning(
                        "任务 %s 的 checkpoint 清理失败，将由后台维护重试",
                        task_id[:8],
                        exc_info=True,
                    )
            self._repo_paths.pop(task_id, None)
            self._run_tasks.pop(task_id, None)

    async def _invoke_graph_with_progress(
        self,
        compiled: Any,
        graph_input: Any,
        run_config: dict[str, Any],
        task: ReviewTask,
    ) -> dict[str, Any]:
        """流式执行图，并在节点开始、结束时立即更新任务进度。"""
        stream = getattr(compiled, "astream", None)
        if not callable(stream):
            # 保留对测试替身以及旧编译器接口的兼容。
            return await compiled.ainvoke(graph_input, config=run_config)

        result: dict[str, Any] | None = None
        async for chunk in stream(
            graph_input,
            config=run_config,
            stream_mode=["debug", "values", "custom"],
            subgraphs=True,
            version="v2",
        ):
            if not isinstance(chunk, dict):
                continue
            chunk_type = chunk.get("type")
            if chunk_type == "values" and not chunk.get("ns"):
                state = chunk.get("data")
                if isinstance(state, dict):
                    result = state
                continue
            if chunk_type == "custom":
                progress = chunk.get("data")
                if isinstance(progress, dict) and progress.get("kind") == "git_progress":
                    self._sync_custom_progress_event(task, progress)
                continue
            if chunk_type != "debug":
                continue
            event = chunk.get("data")
            if not isinstance(event, dict) or event.get("type") not in {"task", "task_result"}:
                continue
            event_type = event["type"]
            payload = event.get("payload")
            if not isinstance(payload, dict) or not isinstance(payload.get("name"), str):
                continue
            self._sync_graph_step_event(
                task,
                payload["name"],
                event_type=event_type,
                timestamp=event.get("timestamp"),
                payload=payload,
            )

        if result is None:
            raise RuntimeError("LangGraph 流式执行未返回最终状态")
        return result

    def _sync_custom_progress_event(
        self,
        task: ReviewTask,
        payload: dict[str, Any],
    ) -> None:
        """把节点内部的长任务进度合并到当前 TaskStep，并立即持久化供 SSE 推送。"""
        node = str(payload.get("node") or "")
        if not node:
            return
        step = next(
            (
                item
                for item in reversed(task.steps)
                if item.name == node and item.status == StepStatus.running
            ),
            None,
        )
        event_time = datetime.now(timezone.utc)
        if step is None:
            task.steps = [item for item in task.steps if item.name != "queued"]
            step = TaskStep(
                name=node,
                status=StepStatus.running,
                started_at=event_time,
            )
            task.steps.append(step)
        step.message = str(payload.get("message") or step.message or "")
        step.progress = TaskStepProgress.model_validate({
            key: payload.get(key)
            for key in ("phase", "operation", "percent", "current", "total", "detail")
        })
        step.updated_at = event_time
        self._touch(task)
        self._persist(task)

    def _sync_graph_step_event(
        self,
        task: ReviewTask,
        node: str,
        *,
        event_type: str,
        timestamp: Any,
        payload: dict[str, Any],
    ) -> None:
        """把 LangGraph 的 task/task_result 事件转换为可查询的 TaskStep。"""
        event_time = _parse_event_time(timestamp)
        if event_type == "task":
            task.steps = [step for step in task.steps if step.name != "queued"]
            task.steps.append(
                TaskStep(
                    name=node,
                    status=StepStatus.running,
                    started_at=event_time,
                    updated_at=event_time,
                )
            )
            mapped_status = _TASK_STATUS_BY_GRAPH_NODE.get(node)
            if mapped_status is not None:
                task.status = mapped_status
                task.review.status = mapped_status
        else:
            step = next(
                (
                    item
                    for item in reversed(task.steps)
                    if item.name == node and item.status == StepStatus.running
                ),
                None,
            )
            if step is None:
                step = TaskStep(name=node, started_at=event_time)
                task.steps.append(step)
            progress = _latest_step_progress(payload)
            failed = bool(payload.get("error")) or (
                progress is not None and progress.get("status") == StepStatus.failed.value
            )
            step.status = StepStatus.failed if failed else StepStatus.completed
            step.message = (
                str(progress.get("message") or "") if progress is not None else step.message
            )
            step.finished_at = event_time
            step.updated_at = event_time
        self._touch(task)
        self._persist(task)

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
                status=StepStatus(step.get("status", StepStatus.completed.value)),
                message=step.get("message", ""),
                finished_at=_parse_event_time(step.get("timestamp")),
            )
            for index, step in enumerate(result.get("step_progress") or [], start=1)
        ]
        self._touch(task)

    def cancel_task(self, task_id: str) -> bool:
        if self._repository:
            cancelled = self._repository.cancel_task(task_id)
            if self._task_queue:
                self._task_queue.cancel_for_task(task_id)
            if self._worker:
                self._worker.cancel_task(task_id)
            return cancelled
        """取消主任务；取消会沿 await 链传播到所有 Unit worker。"""
        run_task = self._run_tasks.get(task_id)
        if run_task is None or run_task.done():
            return False
        run_task.cancel()
        return True

    async def retry_unit(self, task_id: str, unit_id: str) -> ReviewUnitResult:
        """在新的临时 clone 中只重试一个 Unit，并原位替换其聚合结果。"""
        task = self.get_task(task_id)
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
                await _cleanup_repo(Path(repo_path))

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
            self._persist(task)
            return result

    def _touch(self, task: ReviewTask) -> None:
        task.updated_at = datetime.now(timezone.utc)

    def _persist(self, task: ReviewTask) -> None:
        if self._repository:
            self._repository.save_task(task)
        else:
            self._tasks[task.id] = task

    def answer_human_request(
        self,
        task_id: str,
        request_id: str,
        answer: Any,
        *,
        answered_by: str,
    ):
        if not self._repository or not self._task_queue:
            raise ValueError("human request persistence is not configured")
        detail, replay = self._repository.answer_human_request(
            task_id=task_id,
            request_id=request_id,
            answer=answer,
            answered_by=answered_by,
        )
        if not replay:
            self._task_queue.enqueue(
                task_id=task_id,
                payload={"resume": answer.model_dump(mode="json")},
                idempotency_key=f"review:{task_id}:human:{request_id}",
            )
            self._ensure_worker_started()
        return detail, replay


async def _cleanup_repo(repo_path: Path) -> bool:
    """清理克隆的临时仓库目录。"""
    return await asyncio.to_thread(cleanup_workspace, repo_path)


def _extract_interrupt_payload(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    interrupts = result.get("__interrupt__") or ()
    if not interrupts:
        return None
    first = interrupts[0]
    value = getattr(first, "value", first)
    return value if isinstance(value, dict) else None


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


def _parse_event_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _latest_step_progress(payload: dict[str, Any]) -> dict[str, Any] | None:
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    progress = result.get("step_progress")
    if not isinstance(progress, list):
        return None
    return next((item for item in reversed(progress) if isinstance(item, dict)), None)


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
