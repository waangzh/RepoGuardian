import asyncio
import logging
from contextlib import AbstractContextManager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.tracers.langchain import LangChainTracer
from langsmith import Client, tracing_context

from app.agents.providers import LLMProvider
from app.core.config import settings
from app.graph.builder import build_review_graph
from app.graph.checkpointer import (
    delete_thread_checkpoints,
    get_checkpointer,
    review_thread_config,
)
from app.graph.state import ReviewState
from app.models.review import (
    ExecutionBudget,
    PatchStatus,
    PatchValidationResult,
    ReviewCreateRequest,
    RepositorySnapshot,
    ReviewPreviewRequest,
    ReviewPreviewResponse,
    ValidationBackendPreview,
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
from app.models.review_runtime import TaskRuntimeRetryResult, TaskRuntimeReport
from app.services.report_service import ReportService
from app.services.review_rebuild import rebuild_task_from_state
from app.services.review_planner import DeterministicReviewPlanner
from app.services.review_run_budget import (
    ReviewRepositoryRunBudgetStore,
    ReviewRunBudget,
    activate_run_budget,
)
from app.services.review_repository import ReviewRepository
from app.services.task_queue import ClaimedJob, DatabaseTaskQueue, NonRetryableJobError, ReviewWorker
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
    "task_review_runtime": TaskStatus.reviewing,
}


class ReviewDeadlineExceeded(TimeoutError):
    """冻结方案的总期限，区别于上游网络超时。"""


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
        # 仅保留旧构造签名兼容性；production Review Plane 不保存也不注入
        # 任何目标仓库命令执行能力。
        del command_executor
        self._validation_backend_selector = validation_backend_selector or ValidationBackendSelector()
        self._repository = repository
        self._task_queue = task_queue or (DatabaseTaskQueue() if repository else None)
        self._worker = ReviewWorker(self._task_queue, self._handle_job) if self._task_queue else None
        self._worker_task: asyncio.Task[None] | None = None
        self._tasks: dict[str, ReviewTask] = {}
        self._run_tasks: dict[str, asyncio.Task[None]] = {}
        self._repo_paths: dict[str, Path] = {}
        self._previews: dict[str, dict] = {}
        self._preview_prepare_lock = asyncio.Lock()
        self._dispositions: dict[str, dict] = {}

    def create_task(self, request: ReviewCreateRequest, *, configuration=None, preview_id=None,
                    dispatch=True) -> ReviewTask:
        """创建审查任务并异步启动图执行。"""
        from app.graph.nodes.task_review_runtime import task_runtime_configuration
        task = ReviewTask(
            id=uuid4().hex,
            status=TaskStatus.queued,
            pr_url=str(request.pr_url),
            model=request.model,
            engine=request.engine,
            task_runtime_configuration=(configuration or task_runtime_configuration(request.model)
                                        if request.engine == "task_review_v1" else None),
            mode=request.mode,
            generate_patches=request.generate_patches,
            validation_backend=request.validation_backend,
            validation_profile=request.validation_profile,
            review={"mode": request.mode, "status": TaskStatus.queued, "completed": False},
            validation=[],
            steps=[TaskStep(name="queued", message="任务已创建")],
        )
        logger.info("📋 创建审查任务 %s（PR: %s, 模型: %s）", task.id[:8], request.pr_url, request.model or "默认")
        if self._repository and self._task_queue:
            if preview_id:
                self._repository.create_task(task, preview_id=preview_id, enqueue=True)
            else:
                self._repository.create_task(task)
                self._task_queue.enqueue(task_id=task.id, idempotency_key=f"review:{task.id}:initial")
            if dispatch:
                self._ensure_worker_started()
        else:
            self._tasks[task.id] = task
            if preview_id:
                self._previews[preview_id]["consumed_task_id"] = task.id
            if dispatch:
                self._run_tasks[task.id] = asyncio.create_task(self._run_graph(task.id))
        return task

    async def _create_and_dispatch(self, request, *, configuration=None, preview_id=None):
        task = await asyncio.to_thread(self.create_task, request, configuration=configuration,
                                       preview_id=preview_id, dispatch=False)
        if self._repository and self._task_queue:
            self._ensure_worker_started()
        else:
            self._run_tasks[task.id] = asyncio.create_task(self._run_graph(task.id))
        return task

    async def create_confirmed_task(self, request: ReviewCreateRequest) -> ReviewTask:
        from pydantic import ValidationError
        from app.services.preview_confirmation import validate_confirmation, pr_fingerprint
        if not request.preview_id:
            return await self._create_and_dispatch(request)
        record = (await asyncio.to_thread(self._repository.get_preview, request.preview_id)
                  if self._repository else self._previews.get(request.preview_id))
        if not record or record.get("pr_url") != str(request.pr_url):
            raise ValueError("review_plan_stale")
        try:
            response = ReviewPreviewResponse.model_validate(record["response"])
        except (ValidationError, KeyError, TypeError):
            raise ValueError("review_plan_stale") from None
        expected = response.snapshot
        if (response.plan_hash != request.plan_hash or expected is None
                or expected.head_sha != request.expected_head_sha
                or expected.effective_base_sha != request.expected_effective_base_sha):
            raise ValueError("review_plan_stale")
        if record.get("consumed_task_id"):
            task = await asyncio.to_thread(self.get_task, record["consumed_task_id"])
            if task is None:
                raise ValueError("review_plan_stale")
            return task
        expiry = response.expires_at
        if expiry is None:
            raise ValueError("review_plan_stale")
        if (expiry.replace(tzinfo=timezone.utc) if expiry.tzinfo is None else expiry) <= datetime.now(timezone.utc):
            raise ValueError("review_plan_expired")
        configuration, expected_pr = validate_confirmation(record, request)
        if request.model and request.model != configuration["model"]:
            raise ValueError("review_plan_model_mismatch")
        pr = await self._github_tool.fetch_pr(str(request.pr_url))
        if pr_fingerprint(pr) != expected_pr:
            raise ValueError("review_plan_stale")
        # 网络等待期间服务端配置也可能变化；只复核本地指纹，不重做准备。
        configuration, _ = validate_confirmation(record, request)
        return await self._create_and_dispatch(request.model_copy(update={"model": configuration["model"]}),
                                configuration=configuration, preview_id=request.preview_id)

    def get_task(self, task_id: str) -> ReviewTask | None:
        if self._repository:
            return self._repository.get_task(task_id)
        return self._tasks.get(task_id)

    def list_tasks(self, *, status: str | None, page: int, page_size: int,
                   q=None, repository=None, pr_number=None, head_sha=None):
        if not self._repository:
            items = list(self._tasks.values())
            if status:
                items = [item for item in items if item.status.value == status]
            if q:
                items = [item for item in items if q.casefold() in (item.pr_url + item.id).casefold()]
            if repository:
                items = [item for item in items if f"github.com/{repository}/pull/" in item.pr_url]
            if pr_number is not None:
                items = [item for item in items if item.pr_url.endswith(f"/pull/{pr_number}")]
            if head_sha:
                items = [item for item in items if item.pr and item.pr.head.sha == head_sha]
            items.sort(key=lambda item: item.created_at, reverse=True)
            start = (page - 1) * page_size
            from app.models.persistence import ReviewTaskListResponse
            return ReviewTaskListResponse(
                items=items[start:start + page_size], total=len(items), page=page, page_size=page_size
            )
        return self._repository.list_tasks(status=status, page=page, page_size=page_size,
            q=q, repository=repository, pr_number=pr_number, head_sha=head_sha)

    def get_unit_detail(self, task_id: str, unit_id: str):
        return self._repository.get_unit(task_id, unit_id) if self._repository else None

    def get_issue_detail(self, task_id: str, issue_id: str):
        return self._repository.get_issue(task_id, issue_id) if self._repository else None

    def get_patch_detail(self, task_id: str, patch_id: str):
        return self._repository.get_patch(task_id, patch_id) if self._repository else None

    def get_validation_detail(self, task_id: str, validation_id: str):
        return self._repository.get_validation(task_id, validation_id) if self._repository else None

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

    async def _prepare_preview_inputs(self, pr, configuration, progress, cancel_event):
        from app.services.preview_input_cache import PreviewInputCache, input_cache_key

        async def report(phase, message, **extra):
            if progress:
                await asyncio.to_thread(progress, phase, message, **extra)

        await report("cache", "等待并检查固定快照准备缓存")
        async with self._preview_prepare_lock:
            cache = PreviewInputCache(self._repository) if self._repository else None
            key = input_cache_key(pr, configuration)
            try:
                cached = await asyncio.to_thread(cache.get, key) if cache else None
            except Exception as exc:
                logger.warning("读取预览缓存失败：%s", type(exc).__name__)
                cached = None
            if cached is not None:
                await report("cache", "已复用相同快照的仓库输入与索引", cache_hit=True)
                return cached

            def git_progress(event):
                if progress:
                    progress(event.phase, event.message, percent=event.percent, operation=event.operation)

            await report("clone", "正在克隆仓库")
            # 无回调时保持旧工具适配器签名兼容。
            options = {"progress_callback": git_progress, "cancel_event": cancel_event} if progress or cancel_event else {}
            repo_path, diff_text = await asyncio.to_thread(self._git_tool.clone_and_diff, pr, **options)
            try:
                options = {"call_resolution_version": configuration["call_resolution_version"]}
                if progress:
                    options["progress"] = progress
                index = await RepoIndexer().execute(repo_path=str(repo_path), **options)
                diff_context = await self._diff_context(repo_path, pr)
                prepared = {"diff": diff_text, "index": index, "diff_context": diff_context}
            finally:
                # 取消／失租时也必须清理；不能让进度回调异常跳过清理。
                try:
                    await report("cleanup", "正在清理临时仓库")
                finally:
                    await _cleanup_repo(Path(repo_path))
            if cache:
                try:
                    await asyncio.to_thread(cache.put, key, prepared)
                except Exception as exc:
                    logger.warning("写入预览缓存失败：%s", type(exc).__name__)
            return prepared

    async def preview(self, request: ReviewPreviewRequest, *, persist=True,
                      progress=None, cancel_event=None) -> ReviewPreviewResponse:
        """只执行 PR 获取、diff 解析和确定性规划。"""
        pr_url = str(request.pr_url)
        from app.graph.nodes.task_review_runtime import task_runtime_configuration
        from app.services.review_plan import normalize_plan
        configuration = task_runtime_configuration(request.model)
        options = normalize_plan(request.plan, configuration["model"])
        if progress:
            await asyncio.to_thread(progress, "github", "正在获取 PR 信息")
        pr = await self._github_tool.fetch_pr(pr_url)
        # Preview 与正式执行共享 clone + 只读索引输入，保证 related_files 和
        # fingerprint 一致；这一过程不运行仓库代码。
        prepared = await self._prepare_preview_inputs(pr, configuration, progress, cancel_event)
        if progress:
            await asyncio.to_thread(progress, "planning", "正在生成审查方案")
        return await self._build_preview(request, pr_url, pr, configuration, options, prepared, persist)

    async def _build_preview(self, request, pr_url, pr, configuration, options, prepared, persist):
        from app.services.review_plan import apply_scope, configure_runtime, preview_hash
        from app.services.review_input_context import build_pr_intent
        from app.services.preview_confirmation import policy_fingerprint
        planning_policy = policy_fingerprint()
        changed_files = self._diff_parser.parse(prepared["diff"])
        index = prepared["index"]
        planner = DeterministicReviewPlanner()
        # Preview 与正式执行使用同一内容基准：声明 base 只保留 PR 身份。
        diff_context = prepared["diff_context"]
        plan = planner.plan(
            changed_files,
            base_sha=diff_context.get("effective_base_sha") or pr.base.sha,
            head_sha=pr.head.sha,
            file_index=index["file_index"],
            symbol_index=index["symbol_index"],
            repository_graph=index["repository_graph"],
            model=configuration["model"],
            provider=configuration["provider"],
            pr_intent_hash=build_pr_intent(pr.model_dump(mode="json"))["intent_hash"],
        )
        plan = apply_scope(plan, options)
        snapshot = {"head_sha": pr.head.sha,
                    "effective_base_sha": diff_context.get("effective_base_sha") or pr.base.sha,
                    "declared_base_sha": pr.base.sha}
        plan_hash = preview_hash(options, snapshot, configuration, plan)
        frozen = configure_runtime(configuration, options, snapshot, plan_hash)
        from app.services.fingerprints import stable_hash
        frozen["initial_plan_hash"] = stable_hash(plan.model_dump(mode="json"))
        frozen["initial_plan"] = plan.model_dump(mode="json")
        backend = self._validation_backend_selector.select(
            request.validation_backend.value,
            request.mode,
        )
        capabilities = await backend.capabilities(RepositorySnapshot(
            language=index.get("project_meta", {}).get("language", "unknown"),
            languages=list(index.get("project_meta", {}).get("languages") or []),
            language_counts=dict(
                index.get("project_meta", {}).get("language_counts") or {}
            ),
            is_mixed_language=bool(
                index.get("project_meta", {}).get("is_mixed_language", False)
            ),
            framework=index.get("project_meta", {}).get("framework"),
            test_framework=index.get("project_meta", {}).get("test_framework"),
            total_files=len(index["file_index"]),
        ))
        response = ReviewPreviewResponse(
            preview_id=uuid4().hex, plan_hash=plan_hash, snapshot=snapshot,
            effective_plan=options, expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
            mode=request.mode,
            changed_file_count=len(plan.changed_files),
            included_file_count=sum(item.included for item in plan.changed_files),
            changed_files=plan.changed_files,
            review_units=plan.review_units,
            excluded_files=plan.excluded_files,
            matched_rules=plan.matched_rules,
            risk_tags=plan.risk_tags,
            planning_model_calls=sum(
                planner.planning_model_calls(unit, changed_files)
                for unit in plan.review_units
            ),
            estimated_model_calls=sum(
                planner.estimated_model_calls(unit, changed_files)
                for unit in plan.review_units
            ),
            max_model_calls=sum(
                planner.max_model_calls(unit) for unit in plan.review_units
            ),
            estimated_tokens=sum(unit.estimated_tokens for unit in plan.review_units),
            # Repair is an independent workflow and never part of previewed
            # production Review Plane capacity.
            patch_generation_enabled=False,
            validation_backend=ValidationBackendPreview(
                name=ValidationBackend(backend.name),
                available=capabilities.available,
                unavailable_reason=capabilities.unavailable_reason,
            ),
            warnings=plan.warnings,
        )
        if persist:
            from app.services.preview_confirmation import freeze_confirmation
            record = {"pr_url": pr_url, "response": response.model_dump(mode="json"),
                      "configuration": frozen,
                      "confirmation": freeze_confirmation(pr, configuration, request, response, planning_policy=planning_policy)}
            if self._repository:
                await asyncio.to_thread(self._repository.save_preview, response.preview_id,
                                        record, response.expires_at)
            else:
                self._previews[response.preview_id] = record
        return response

    async def _diff_context(self, repo_path: str | Path, pr: Any) -> dict[str, str]:
        """按 PR 身份重新计算内容基准；共享 Git 实例不保存跨任务的基准状态。

        ``GitTool.review_diff_context`` 自身把"无法确定唯一基准"降级为空字典，
        因此这里不吞异常：锁定快照的基准不一致必须显式失败。
        """
        reader = getattr(self._git_tool, "review_diff_context", None)
        if not callable(reader):
            return {}
        return await asyncio.to_thread(reader, repo_path, pr.base.sha, pr.head.sha) or {}

    async def _handle_job(self, job: ClaimedJob) -> None:
        if job.kind == "review":
            await self._run_graph(job.task_id, job=job)
            return
        if job.kind == "unit_retry":
            raise NonRetryableJobError("legacy unit_retry jobs are retired; historical tasks are read-only")
        raise ValueError(f"unsupported worker job kind: {job.kind}")

    def _ensure_worker_started(self) -> None:
        if self._worker is not None and (self._worker_task is None or self._worker_task.done()):
            self._worker_task = asyncio.create_task(self._worker.run_forever())

    async def _run_graph(self, task_id: str, *,
                         job: ClaimedJob | None = None) -> None:
        """执行 LangGraph 审查流程的核心方法。

        1. 构建初始状态字典（ReviewState），注入所有工具实例
        2. 编译并执行 StateGraph
        3. 成功 → 将结果同步到 ReviewTask
        4. 失败 → 标记任务状态为 failed
        5. 始终清理临时克隆仓库
        """
        task = self.get_task(task_id) or self._tasks[task_id]
        if task.engine != "task_review_v1":
            raise NonRetryableJobError("legacy_units engine is retired; historical tasks are read-only")
        if task.status == TaskStatus.cancelled:
            return
        if (task.engine == "task_review_v1" and self._repository is None
                and self._github_tool is None and self._provider is None):
            await self._reject_unwired_task_review_v1(task)
            return
        task.status = TaskStatus.planning
        task.review.status = task.status
        task.phase = ReviewPhase.prepare
        task.error = None
        self._touch(task)
        self._persist(task)

        logger.info("🚀 开始执行审查图，任务 %s", task_id[:8])

        initial_state: ReviewState = {
            "task_id": task_id,
            "engine": task.engine,
            "task_runtime_configuration": task.task_runtime_configuration,
            "task_runtime_retry_unit_id": task.task_runtime_retry_unit_id,
            "mode": task.mode.value,
            "status": TaskStatus.planning.value,
            "created_at": task.created_at.isoformat(),
            "updated_at": task.updated_at.isoformat(),
            "generate_patches": task.generate_patches,
            "validation_backend": task.validation_backend.value,
            "validation_profile": task.validation_profile,
            "validation_results": [item.model_dump(mode="json") for item in task.validation],
            "warnings": (
                [
                    "修复与动态验证已从主 Review lifecycle 解耦；本次任务仅执行严格只读审查"
                ]
                if task.generate_patches or task.validation_backend != ValidationBackend.none
                else []
            ),
            "pr_url": task.pr_url,
            "model": (task.task_runtime_configuration or {}).get("model", task.model),
            "execution_budget": ExecutionBudget().model_dump(),
            "agent_events": [],
            "_github_tool": self._github_tool,
            "_git_tool": self._git_tool,
            "_diff_parser": self._diff_parser,
            "_provider": self._provider,
            "_validation_backend_selector": self._validation_backend_selector,
            "_repo_prepared_callback": lambda path: self._repo_paths.__setitem__(
                task_id, Path(path)
            ),
        }
        if self._repository:
            for key in (
                "_github_tool", "_git_tool", "_diff_parser", "_provider",
                "_validation_backend_selector",
                "_repo_prepared_callback",
            ):
                initial_state.pop(key, None)
            initial_state["_persistence_enabled"] = True
            initial_state["_report_purpose_model_enabled"] = True

        result = None
        interrupted = False
        try:
            graph = build_review_graph()
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
                if job:
                    run_config["configurable"]["review_job_lease"] = {
                        "job_id": job.id, "owner": job.lease_owner, "attempt": job.attempts,
                    }
            tracing, callbacks = _build_langsmith_tracing(run_metadata)
            if callbacks:
                run_config["callbacks"] = callbacks
            logger.info("📊 开始流式执行审查图...")
            run_budget_store = (
                ReviewRepositoryRunBudgetStore(
                    self._repository,
                    lease=run_config.get("configurable", {}).get("review_job_lease"),
                ) if self._repository else None
            )
            run_budget = await ReviewRunBudget.load_or_create(
                task_id,
                **((task.task_runtime_configuration or {}).get("limits") or settings.run_budget_limits()),
                store=run_budget_store,
                recover_running=task.engine != "task_review_v1",
            )
            with activate_run_budget(run_budget):
                with tracing:
                    graph_input: Any = initial_state
                    if self._repository:
                        snapshot = await compiled.aget_state(run_config)
                        if snapshot.values and snapshot.values.get("engine", "legacy_units") != task.engine:
                            raise ValueError("checkpoint engine differs from frozen task")
                        if task.engine == "task_review_v1" and snapshot.values.get("review_plan") and (
                                task.task_runtime_retry_unit_id or snapshot.next):
                            from app.models.review import PullRequestInfo
                            frozen_state = dict(snapshot.values)
                            existing_path = frozen_state.get("repo_path")
                            if not existing_path or not await asyncio.to_thread(Path(existing_path).is_dir):
                                pinned_pr = PullRequestInfo.model_validate(frozen_state["pr_info"])
                                clone, _ = await asyncio.to_thread(self._git_tool.clone_and_diff, pinned_pr)
                                frozen_state["repo_path"] = str(clone)
                                if frozen_state.get("effective_base_sha") and frozen_state["effective_base_sha"] != pinned_pr.base.sha:
                                    actual = await asyncio.to_thread(self._git_tool._run,
                                        [self._git_tool._git, "-C", str(clone), "rev-parse", frozen_state["effective_base_sha"]])
                                    if actual.strip() != frozen_state["effective_base_sha"]:
                                        raise ValueError("frozen effective base unavailable")
                            self._repo_paths[task_id] = Path(frozen_state["repo_path"])
                            await compiled.aupdate_state(run_config, {
                                "repo_path": frozen_state["repo_path"],
                                "task_runtime_retry_unit_id": task.task_runtime_retry_unit_id,
                                "task_runtime_configuration": task.task_runtime_configuration,
                            }, as_node="review_plan")
                            graph_input = None
                        elif "human_required" in snapshot.next or snapshot.values.get("status") == "waiting_for_human":
                            await delete_thread_checkpoints(task_id)
                            initial_state["warnings"].append("旧人工暂停流程已移除，本次重新审查当前 PR 快照")
                        elif not snapshot.next and snapshot.values.get("status") in {
                            TaskStatus.completed.value, TaskStatus.completed_with_warnings.value,
                        }:
                            result = snapshot.values
                        elif snapshot.next:
                            existing_path = snapshot.values.get("repo_path")
                            if not existing_path or await asyncio.to_thread(Path(existing_path).is_dir):
                                graph_input = None
                                if existing_path:
                                    self._repo_paths[task_id] = Path(existing_path)
                            else:
                                await delete_thread_checkpoints(task_id)
                    if result is None:
                        configuration = task.task_runtime_configuration or {}
                        if configuration.get("effective_plan"):
                            from app.services.review_plan import remaining_review_seconds
                            remaining = remaining_review_seconds(configuration, task.created_at)
                            if remaining <= 0:
                                raise ReviewDeadlineExceeded("review_deadline")
                            deadline = asyncio.timeout(remaining)
                            try:
                                async with deadline:
                                    result = await self._invoke_graph_with_progress(compiled, graph_input, run_config, task)
                            except TimeoutError:
                                if deadline.expired():
                                    raise ReviewDeadlineExceeded("review_deadline") from None
                                raise
                        else:
                            result = await self._invoke_graph_with_progress(
                                compiled, graph_input, run_config, task)
                if isinstance(result, dict):
                    result["run_budget"] = run_budget.snapshot()
            if result.get("__interrupt__"):
                raise RuntimeError("只读审查不支持人工中断；未生成完整审查结果")
            logger.info("✅ ainvoke 执行完成，开始同步结果")
            self._sync_result_to_task(task, result)
            self._persist(task)
            if self._repository:
                self._repository.save_issue_lifecycle(task_id, result)
            if task.status == TaskStatus.failed:
                logger.error("❌ 审查任务 %s 失败结束：%s", task_id[:8], task.error)
            elif task.status == TaskStatus.completed_with_warnings:
                logger.warning("审查任务 %s 已完成，有警告", task_id[:8])
            elif task.status == TaskStatus.completed:
                logger.info("🎉 审查任务 %s 完成", task_id[:8])
            else:
                logger.info("审查任务 %s 结束，状态：%s", task_id[:8], task.status.value)
        except ReviewDeadlineExceeded:
            task.status = TaskStatus.completed_with_warnings
            task.review.status = task.status
            task.review.completed = False
            task.warnings.append("review_deadline: 已达到冻结方案期限，未完成范围仍未检查，用量可能未知。")
            if task.task_runtime_report and not (
                    task.task_runtime_report.get("execution_status") == "completed"
                    and task.task_runtime_report.get("termination_reason") == "plan_exhausted"):
                task.task_runtime_report.update(execution_status="completed", termination_reason="deadline",
                                                scope_complete=False, usage_integrity="unknown")
            task.report_markdown = self._report_service.generate(task)
            self._touch(task)
            self._persist(task)
        except asyncio.CancelledError:
            current = self.get_task(task_id)
            user_cancelled = current is not None and current.status == TaskStatus.cancelled
            interrupted = bool(self._repository and not user_cancelled)
            if not interrupted:
                task.status = TaskStatus.cancelled
                task.error = None
                self._touch(task)
                self._persist(task)
            raise
        except Exception as exc:
            message = str(exc).strip() or type(exc).__name__
            retrying = bool(job and job.attempts < job.max_attempts
                            and not isinstance(exc, NonRetryableJobError))
            logger.error("审查任务 %s 本次执行异常（%s），%s：%s", task_id[:8],
                         type(exc).__name__, "等待自动重试" if retrying else "任务失败", message)
            task.status = TaskStatus.queued if retrying else TaskStatus.failed
            task.phase = ReviewPhase.prepare if retrying else ReviewPhase.failed
            task.review.status = task.status
            task.review.completed = False
            task.error = message
            if retrying:
                task.steps.append(TaskStep(name=f"worker_retry_{job.attempts}", status=StepStatus.pending,
                    message=f"第 {job.attempts}/{job.max_attempts} 次执行未完成，将自动重试：{message}"))
            self._touch(task)
            if job and self._repository:
                await asyncio.to_thread(self._repository.save_task, task, lease={
                    "job_id": job.id, "owner": job.lease_owner, "attempt": job.attempts})
            else:
                self._persist(task)
            if self._repository:
                raise
        finally:
            repo_path = (
                Path(result["repo_path"])
                if result and result.get("repo_path")
                else self._repo_paths.get(task_id)
            )
            if repo_path is not None and not interrupted:
                await _cleanup_repo(repo_path)
            if self._repository and not interrupted and task.status in {
                TaskStatus.completed,
                TaskStatus.completed_with_warnings,
                TaskStatus.failed,
                TaskStatus.cancelled,
            } and task.engine != "task_review_v1":
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
        runtime_options = ({"context": {"coordination_repository": self._repository,
                            "task_runtime_provider": self._provider, "task_runtime_git": self._git_tool}}
                           if self._repository is not None else {})
        if not callable(stream):
            # 保留对测试替身以及旧编译器接口的兼容。
            return await compiled.ainvoke(graph_input, config=run_config, **runtime_options)

        result: dict[str, Any] | None = None
        async for chunk in stream(
            graph_input,
            config=run_config,
            stream_mode=["debug", "values", "custom"],
            subgraphs=True,
            version="v2",
            **runtime_options,
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
                elif isinstance(progress, dict) and progress.get("kind") == "coordination_progress":
                    delta = progress.get("state") or {}
                    steps = task.steps
                    self._sync_result_to_task(task, {**(result or {}), **delta})
                    task.steps = steps
                    await asyncio.to_thread(self._persist, task)
                elif isinstance(progress, dict) and progress.get("kind") == "task_runtime_progress":
                    if task.engine != "task_review_v1":
                        raise ValueError("new runtime progress cannot enter legacy task")
                    delta = progress.get("state") or {}
                    task.task_runtime_report = TaskRuntimeReport.model_validate(delta["task_runtime_report"]).model_dump(mode="json")
                    from app.models.review import ReviewCoverage
                    task.coverage = ReviewCoverage.model_validate(delta["review_coverage"])
                    self._touch(task)
                    await asyncio.to_thread(self._persist, task)
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
        if rebuilt.engine != task.engine:
            raise ValueError("graph engine does not match frozen task engine")
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
        task.cross_unit_risk = rebuilt.cross_unit_risk
        task.coordination_plan = rebuilt.coordination_plan
        task.followup_results = rebuilt.followup_results
        task.model_usages = rebuilt.model_usages
        task.model_usage_summary = rebuilt.model_usage_summary
        task.excluded_files = rebuilt.excluded_files
        task.coverage = rebuilt.coverage
        task.run_manifest = rebuilt.run_manifest
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
        task.task_runtime_configuration = rebuilt.task_runtime_configuration
        task.task_runtime_report = rebuilt.task_runtime_report
        task.pr_discussion = rebuilt.pr_discussion
        if task.status == TaskStatus.failed and task.task_runtime_report:
            from app.services.task_review_projection import runtime_failure_summary
            task.error = runtime_failure_summary(task.task_runtime_report.get("diagnostics") or [],
                termination_reason=task.task_runtime_report.get("termination_reason"))
        else:
            task.error = rebuilt.error
        task.task_runtime_retry_unit_id = rebuilt.task_runtime_retry_unit_id
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
            if cancelled and self._task_queue:
                self._task_queue.cancel_for_task(task_id)
            if cancelled and self._worker:
                self._worker.cancel_task(task_id)
            return cancelled
        """取消主任务；取消会沿 await 链传播到所有 Unit worker。"""
        run_task = self._run_tasks.get(task_id)
        if run_task is None or run_task.done():
            return False
        run_task.cancel()
        return True

    async def retry_unit(self, task_id: str, unit_id: str) -> TaskRuntimeRetryResult:
        """只重排显式的未决缺口；旧引擎任务只读，不能重试。"""
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.engine != "task_review_v1":
            raise ValueError("legacy_units engine is retired; historical tasks are read-only")
        if not task.task_runtime_report:
            raise ValueError("task_review_v1 runtime report is missing")
        report = TaskRuntimeReport.model_validate(task.task_runtime_report)
        known_units = {item.get("unit_id") for item in report.work_items}
        if unit_id not in known_units:
            raise KeyError(unit_id)
        if task.status not in {TaskStatus.completed, TaskStatus.completed_with_warnings,
                               TaskStatus.failed}:
            raise ValueError("task runtime must be terminal before retrying a unit")
        if self._repository is None:
            raise ValueError("task runtime retry requires persisted fixed runtime")
        gaps = {item["goal_id"] for item in report.initial_scope_coverage
                if item.get("unit_id") == unit_id and item.get("status") in {"blocked", "not_examined"}}
        requeued = [item["work_item_id"] for item in report.work_items
                    if item.get("unit_id") == unit_id and item.get("state") != "unknown"
                    and gaps.intersection(item.get("goal_ids", []))]
        if not requeued:
            return TaskRuntimeRetryResult(unit_id=unit_id, requeued_work_item_ids=[], task_runtime_report=report)
        task.task_runtime_retry_unit_id = unit_id
        task.status = TaskStatus.queued
        task.phase = ReviewPhase.prepare
        task.error = None
        task.warnings.append("task runtime retry only requeued explicit pending/blocked gaps")
        self._touch(task)
        self._persist(task)
        if self._repository and self._task_queue:
            self._task_queue.enqueue(task_id=task.id,
                idempotency_key=f"review:{task.id}:task-runtime-retry:{unit_id}:{task.updated_at.timestamp()}")
            self._ensure_worker_started()
        else:
            self._run_tasks[task.id] = asyncio.create_task(self._run_graph(task.id))
        return TaskRuntimeRetryResult(unit_id=unit_id, requeued_work_item_ids=requeued,
                                      task_runtime_report=report)

    async def _reject_unwired_task_review_v1(self, task: ReviewTask) -> None:
        """显式拒绝缺少持久化与模型适配器的执行环境，不做静默降级。"""
        task.status = TaskStatus.failed
        task.phase = ReviewPhase.failed
        task.review.completed = False
        task.error = "task_review_v1_production_entry_not_enabled"
        task.warnings.append(
            "task_review_v1 需要持久化仓储与模型 Provider；当前执行环境未接入，已显式失败"
        )
        task.review.status = task.status
        self._touch(task)
        self._persist(task)

    def _touch(self, task: ReviewTask) -> None:
        task.updated_at = datetime.now(timezone.utc)

    def _persist(self, task: ReviewTask) -> None:
        if self._repository:
            self._repository.save_task(task)
        else:
            self._tasks[task.id] = task


async def _cleanup_repo(repo_path: Path) -> bool:
    """清理克隆的临时仓库目录。"""
    return await asyncio.to_thread(cleanup_workspace, repo_path)


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
