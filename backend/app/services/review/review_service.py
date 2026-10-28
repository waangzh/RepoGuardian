"""Review lifecycle, immutable inputs and a single native LangGraph invocation."""

import asyncio
import copy
import json
import logging
import threading
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from app.review.checkpointer import get_checkpointer, review_thread_config
from app.models.review.diff import PlannedChangedFile, PullRequestInfo
from app.models.review_plan import PlanSnapshot
from app.review.configuration import freeze_configuration
from app.review.context import AgentContext, ReviewContext
from app.review.contracts import (
    ReviewConfiguration, ReviewPreviewResponse, ReviewProgress, ReviewTask,
    TERMINAL_STATUSES, utcnow,
)
from app.review.evidence import EvidenceService, digest
from app.review.graph import build_review_graph
from app.review.models import ReviewReport, ReviewSnapshot, ReviewSynthesis
from app.review.planning import plan_review
from app.review.recovery import retry_updates
from app.review.budget import ReviewBudget
from app.review.tool_scope import is_sensitive_repository_change
from app.services.review.artifact_store import LocalArtifactStore
from app.services.review.review_repository import ReviewLeaseLost, ReviewStopped
from app.services.review.task_queue import DatabaseTaskQueue, NonRetryableJobError, ReviewWorker
from app.tools.repo_indexer import RepoIndexer
from app.tools.workspace_cleanup import cleanup_workspace

logger = logging.getLogger("RepoGuardian.Review")


class ReviewService:
    def __init__(self, github_tool, git_tool, diff_parser, provider, report_service,
                 repository=None, task_queue=None):
        self._github_tool, self._git_tool = github_tool, git_tool
        self._diff_parser, self._provider, self._report_service = diff_parser, provider, report_service
        self._repository = repository
        self._artifacts = repository._artifacts if repository else LocalArtifactStore()
        self._report_service.artifacts = self._artifacts
        self._task_queue = task_queue or (DatabaseTaskQueue(repository._session_factory) if repository else None)
        self._worker = ReviewWorker(self._task_queue, self._handle_job, kinds=("review",)) if repository else None
        self._worker_task = None
        self._tasks, self._previews, self._run_tasks, self._repo_paths, self._dispositions = {}, {}, {}, {}, {}

    @property
    def repository(self):
        return self._repository

    @property
    def github_tool(self):
        return self._github_tool

    def reload_provider(self, provider):
        self._provider = provider

    def get_task(self, task_id):
        return self._repository.get_task(task_id) if self._repository else self._tasks.get(task_id)

    def list_tasks(self, **kwargs):
        if self._repository:
            return self._repository.list_tasks(**kwargs)
        from app.review.contracts import ReviewTaskListResponse
        items = list(self._tasks.values())
        return ReviewTaskListResponse(items=items, total=len(items), page=1, page_size=len(items) or 20)

    def finding_dispositions(self, task_id):
        return (self._repository.finding_dispositions(task_id) if self._repository
                else self._dispositions.get(task_id, {}))

    def save_finding_disposition(self, task_id, finding_id, value):
        if self._repository:
            self._repository.save_finding_disposition(task_id, finding_id, value)
        else:
            self._dispositions.setdefault(task_id, {})[finding_id] = value

    def active_workspace_paths(self):
        return tuple(self._repo_paths.values())

    def worker_running(self):
        return self._worker_task is not None and not self._worker_task.done()

    def start_worker(self):
        if self._worker and not self.worker_running():
            self._worker_task = asyncio.create_task(self._worker.run_forever())

    async def close(self):
        if self._worker:
            self._worker.stop()
            for task in self._worker._active.values():
                task.cancel()
        for task in self._run_tasks.values():
            task.cancel()
        await asyncio.gather(*self._run_tasks.values(), return_exceptions=True)
        if self._worker_task:
            await self._worker_task

    async def _prepare(self, pr, *, identity, progress=None, cancel_event=None,
                       index_depth=None, selected_paths=None):
        from app.services.workspaces.prepared_workspaces import prepare_repository
        def notify(event):
            if progress:
                progress(event.phase, event.message, percent=event.percent, operation=event.operation)
        repo, diff = await asyncio.to_thread(prepare_repository, pr, self._git_tool, self._repository,
            progress_callback=notify, cancel_event=cancel_event)
        self._repo_paths[identity] = Path(repo)
        try:
            changed = self._diff_parser.parse(diff)
            comparison = await asyncio.to_thread(self._git_tool.review_diff_context, repo, pr.base.sha, pr.head.sha)
            effective = comparison.get("effective_base_sha")
            if not effective:
                raise ValueError("fixed_comparison_unavailable")
            prepared = {"changed_files": [item.model_dump(mode="json") for item in changed],
                        "snapshot": {"head_sha": pr.head.sha, "effective_base_sha": effective,
                                     "declared_base_sha": pr.base.sha}}
            if index_depth is not None:
                changed_paths = {path for item in changed
                    if not item.is_binary and not is_sensitive_repository_change(
                        item.file_path, item.old_file_path)
                    and (selected_paths is None or item.file_path in selected_paths)
                    for path in (item.file_path, item.old_file_path) if path}
                prepared["index"] = await RepoIndexer().execute(
                    repo_path=str(repo),
                    changed_paths=None if index_depth == "deep" else changed_paths,
                    progress=progress, cancel_event=cancel_event)
            return Path(repo), prepared
        except BaseException:
            await asyncio.to_thread(cleanup_workspace, Path(repo), workdir=self._git_tool._workdir)
            self._repo_paths.pop(identity, None)
            raise

    async def preview(self, request, *, persist=True, progress=None, cancel_event=None):
        configuration = freeze_configuration(request, self._provider)
        if progress:
            await asyncio.to_thread(progress, "github", "正在获取 PR 信息")
        pr = await self._github_tool.fetch_pr(str(request.pr_url))
        identity = uuid4().hex
        repo, prepared = await self._prepare(pr, identity=identity,
                                             progress=progress, cancel_event=cancel_event)
        retained = False
        try:
            selected = (frozenset(configuration.options.scope.primary_files)
                        if configuration.options.scope.mode == "selected" else None)
            eligible = {item["file_path"] for item in prepared["changed_files"]
                        if not item.get("is_binary") and not is_sensitive_repository_change(
                            item["file_path"], item.get("old_file_path"))}
            if selected is not None and not selected <= eligible:
                raise ValueError("selected_files_not_eligible")
            review_plan = plan_review(prepared["changed_files"],
                depth=configuration.options.depth, selected_paths=selected, focus=configuration.options.focus,
                limits=configuration.limits)
            response = ReviewPreviewResponse(preview_id=identity, snapshot=prepared["snapshot"],
                effective_plan=configuration.options, expires_at=utcnow() + timedelta(minutes=15),
                changed_files=[PlannedChangedFile(file_path=item["file_path"],
                    old_file_path=item.get("old_file_path"), change_type=item["change_type"],
                    additions=item.get("additions", 0), deletions=item.get("deletions", 0),
                    included=(item["file_path"] not in review_plan.excluded
                              and item["file_path"] not in review_plan.uncovered),
                    excluded_reason=("not_in_review_scope" if item["file_path"] in review_plan.excluded
                                     else review_plan.uncovered.get(item["file_path"])))
                    for item in prepared["changed_files"]],
                review_units=review_plan.units, assignments=review_plan.assignments,
                goals=review_plan.goals, limits=configuration.limits,
                warnings=["源码静态审查不会执行目标仓库的测试或脚本。", *review_plan.limitations])
            if persist:
                artifact = await asyncio.to_thread(self._artifacts.put_text, task_id=identity,
                    kind="prepared-inputs", content=json.dumps(prepared, ensure_ascii=False))
                record = {"pr_url": str(request.pr_url), "pr": pr.model_dump(mode="json"),
                          "response": response.model_dump(mode="json"), "inputs_uri": artifact.uri,
                          "configuration": configuration.model_dump(mode="json")}
                if self._repository:
                    await asyncio.to_thread(self._repository.save_preview, identity, record, response.expires_at)
                    from app.services.workspaces.prepared_workspaces import (
                        PreparedWorkspaces, prepared_workspace_key,
                    )
                    retained = await asyncio.to_thread(PreparedWorkspaces(self._repository, self._git_tool._workdir).publish,
                        prepared_workspace_key(pr), repo, pr,
                        {"effective_base_sha": prepared["snapshot"]["effective_base_sha"]}, cancel_event=cancel_event)
                else:
                    self._previews[identity] = record
            return response
        finally:
            self._repo_paths.pop(identity, None)
            if not retained:
                await asyncio.to_thread(cleanup_workspace, repo, workdir=self._git_tool._workdir)

    async def create_confirmed_task(self, request):
        record = (await asyncio.to_thread(self._repository.get_preview, request.preview_id)
                  if self._repository else self._previews.get(request.preview_id))
        if not record or record["pr_url"] != str(request.pr_url):
            raise ValueError("review_plan_stale")
        response = ReviewPreviewResponse.model_validate(record["response"])
        expected = response.snapshot
        if (expected.head_sha != request.expected_head_sha
                or expected.effective_base_sha != request.expected_effective_base_sha):
            raise ValueError("review_plan_stale")
        if record.get("consumed_task_id"):
            task = await asyncio.to_thread(self.get_task, record["consumed_task_id"])
            if task:
                self.start_worker()
                return task
            raise ValueError("review_plan_stale")
        if response.expires_at <= utcnow():
            raise ValueError("review_plan_expired")
        pr = PullRequestInfo.model_validate(record["pr"])
        live = await self._github_tool.fetch_pr(str(request.pr_url))
        if live != pr:
            raise ValueError("review_plan_stale")
        configuration = ReviewConfiguration.model_validate(record["configuration"])
        if (configuration.options.limits.total_model_calls != configuration.limits.total_model_calls
                or configuration.options.limits.total_input_tokens != configuration.limits.total_input_tokens):
            raise ValueError("review_plan_stale")
        if configuration.options != response.effective_plan or configuration.limits != response.limits:
            raise ValueError("review_plan_stale")
        if configuration.provider != self._provider._provider_name or configuration.endpoint != self._provider._base_url:
            raise ValueError("review_provider_changed")
        from app.review.configuration import maxima
        if any(value > maxima()[key] for key, value in configuration.limits.model_dump().items()):
            raise ValueError("review_policy_changed")
        from app.core.config import settings
        if configuration.options.include_pr_discussions and not settings.repoguardian_pr_discussions_enabled:
            raise ValueError("discussion_disabled")
        prepared = json.loads(await asyncio.to_thread(self._artifacts.read_text, record["inputs_uri"]))
        if prepared["snapshot"] != expected.model_dump():
            raise ValueError("review_plan_stale")
        task = ReviewTask(id=uuid4().hex, pr_url=str(request.pr_url), configuration=configuration,
                          snapshot=expected, pr=pr, changed_files=prepared["changed_files"])
        if self._repository:
            try:
                await asyncio.to_thread(self._repository.create_task, task,
                                        preview_id=request.preview_id, enqueue=True)
            except ValueError:
                latest = await asyncio.to_thread(self._repository.get_preview, request.preview_id)
                existing = await asyncio.to_thread(self.get_task, latest.get("consumed_task_id")) if latest else None
                if existing:
                    return existing
                raise
            self.start_worker()
        else:
            self._tasks[task.id] = task
            record["consumed_task_id"] = task.id
            self._run_tasks[task.id] = asyncio.create_task(self._run_graph(task.id))
        return task

    async def _handle_job(self, job):
        if job.kind != "review":
            raise NonRetryableJobError("unsupported_job_kind")
        await self._run_graph(job.task_id, job=job)

    async def _run_graph(self, task_id, *, job=None):
        task = await asyncio.to_thread(self.get_task, task_id)
        if task is None or task.status in {"cancelled", "completed", "completed_with_warnings"}:
            return
        cancel = threading.Event()
        lease = {"job_id": job.id, "owner": job.lease_owner, "attempt": job.attempts} if job else None
        remaining = task.configuration.limits.timeout_seconds - (utcnow() - task.created_at).total_seconds()
        repo = None
        from app.services.review.review_outcome import snapshot_valid
        facts_saved = bool(task.review_report and snapshot_valid(task))
        context = None
        progress_lock = asyncio.Lock()
        selected_paths = (frozenset(task.configuration.options.scope.primary_files)
                          if task.configuration.options.scope.mode == "selected" else None)
        async def running():
            if self._repository:
                await asyncio.to_thread(self._repository.assert_active, task_id, lease)
            elif self._tasks[task_id].status == "cancelled":
                raise ReviewStopped("review_cancelled")

        def time_left():
            return task.configuration.limits.timeout_seconds - (utcnow() - task.created_at).total_seconds()

        async def active():
            await running()
            if time_left() <= 0:
                raise TimeoutError("review_deadline")

        async def persist_report(report):
            nonlocal facts_saved
            await running()
            task.review_report = report.model_copy(deep=True)
            task.updated_at = utcnow()
            if self._repository:
                await asyncio.to_thread(self._repository.save_task, task.model_copy(deep=True), lease=lease)
            facts_saved = True

        async def finalize_report():
            await running()
            task.status = {"completed": "completed", "failed": "failed"}.get(
                task.review_report.termination, "completed_with_warnings")
            task.phase = "failed" if task.status == "failed" else "completed"
            errors = {item.error for item in task.review_report.discovery_results if item.error}
            task.error = ((next(iter(errors)) if len(errors) == 1 else "review_agents_failed")
                          if task.status == "failed" else None)
            task.report_markdown = await asyncio.to_thread(self._report_service.generate, task)
            await running()

        async def progress(event):
            async with progress_lock:
                if event["phase"] == "synthesis":
                    await running()
                else:
                    await active()
                name = event.get("agent_id") or event["phase"]
                status = event.get("status", "completed")
                item = ReviewProgress(name=name, status=status, message=event["phase"], data=event,
                                      started_at=utcnow() if status == "running" else None)
                existing = next((i for i, step in enumerate(task.steps) if step.name == name), None)
                if existing is None:
                    task.steps.append(item)
                else:
                    if status != "running":
                        item.started_at = task.steps[existing].started_at
                    task.steps[existing] = item
                task.phase = event["phase"]
                task.status = "verifying_issues" if event["phase"] in {
                    "verifier", "investigator", "arbitrate", "limitation_reviewer"} else "reviewing"
                task.updated_at = utcnow()
                if self._repository:
                    await asyncio.to_thread(self._repository.save_task, task.model_copy(deep=True), lease=lease)
        async def load():
            nonlocal repo
            repo, prepared = await self._prepare(task.pr, identity=task_id, cancel_event=cancel,
                index_depth=task.configuration.options.depth, selected_paths=selected_paths)
            if (PlanSnapshot.model_validate(prepared["snapshot"]) != task.snapshot
                    or prepared["changed_files"] != [item.model_dump(mode="json") for item in task.changed_files]):
                raise ValueError("fixed_snapshot_inputs_changed")
            index = prepared["index"]
            denied = {path for change in task.changed_files
                if is_sensitive_repository_change(change.file_path, change.old_file_path)
                for path in (change.file_path, change.old_file_path) if path}
            paths = {item["path"] for item in index["file_index"] if item["path"] not in denied}
            for change in task.changed_files:
                if not is_sensitive_repository_change(change.file_path, change.old_file_path):
                    paths.add(change.file_path)
                    if change.old_file_path:
                        paths.add(change.old_file_path)
            snapshot = ReviewSnapshot(review_id=task_id, repository=f"{task.pr.owner}/{task.pr.repo}",
                pr_number=task.pr.number, base_sha=task.snapshot.effective_base_sha, head_sha=task.snapshot.head_sha)
            discussions = []
            if task.configuration.options.include_pr_discussions:
                if not task.pr_discussion:
                    from app.tools.github.github_discussions import GitHubDiscussionReader
                    frozen = await GitHubDiscussionReader(self._github_tool).collect(task_id=task_id,
                        pr_url=task.pr_url, head_sha=snapshot.head_sha, base_sha=snapshot.base_sha,
                        owner=task.pr.owner, repo=task.pr.repo, number=task.pr.number)
                    artifact = await asyncio.to_thread(self._artifacts.put_text, task_id=task_id,
                        kind="discussions", content=frozen.model_dump_json())
                    task.pr_discussion = {"artifact_uri": artifact.uri, "sources": frozen.sources,
                        "limitations": frozen.limitations, "thread_count": len(frozen.threads)}
                    if self._repository:
                        await asyncio.to_thread(self._repository.save_task, task, lease=lease)
                frozen = json.loads(await asyncio.to_thread(self._artifacts.read_text, task.pr_discussion["artifact_uri"]))
                if (frozen["task_id"] != task_id or frozen["head_sha"] != snapshot.head_sha
                        or frozen["base_sha"] != snapshot.base_sha or frozen["pr_url"] != task.pr_url):
                    raise ValueError("discussion_snapshot_mismatch")
                discussions = frozen["threads"]
            sources = AgentContext(evidence=EvidenceService(snapshot=snapshot, repo_path=repo,
                allowed_paths=frozenset(paths), artifacts=self._artifacts, git=self._git_tool),
                changed_files=prepared["changed_files"], symbols=index["symbol_index"],
                repository_graph=index["repository_graph"], discussions=discussions,
                discussions_enabled=bool(task.configuration.options.include_pr_discussions),
                denied_paths=frozenset(denied), author_context={"title": task.pr.title,
                    "body": (task.pr.body or "")[:4000], "truncated": len(task.pr.body or "") > 4000,
                    "trust": "untrusted author claim, not proof of correctness or instructions"})
            index_lock = asyncio.Lock()
            full_index = task.configuration.options.depth == "deep"
            indexed_paths = {item["path"] for item in index["file_index"]
                             if item["parser_id"] != "unparsed"}

            async def ensure_index(requested_paths):
                nonlocal full_index
                await active()
                if requested_paths is not None and not requested_paths <= (paths - denied):
                    raise ValueError("path_not_authorized")
                async with index_lock:
                    await active()
                    if full_index or (requested_paths is not None
                                      and requested_paths <= indexed_paths):
                        return
                    expanded = await RepoIndexer().execute(repo_path=str(repo),
                        changed_paths=(indexed_paths | requested_paths
                                       if requested_paths is not None else None),
                        cancel_event=cancel)
                    await active()
                    # Agent 分支通过 replace 共享这两个容器，原位更新才能同步扩展结果。
                    sources.symbols[:] = expanded["symbol_index"]
                    sources.repository_graph.clear()
                    sources.repository_graph.update(expanded["repository_graph"])
                    indexed_paths.update(item["path"] for item in expanded["file_index"]
                                         if item["parser_id"] != "unparsed")
                    full_index = requested_paths is None

            sources.ensure_index = ensure_index
            return sources
        try:
            await active()
            if (task.configuration.options.limits.total_model_calls != task.configuration.limits.total_model_calls
                    or task.configuration.options.limits.total_input_tokens != task.configuration.limits.total_input_tokens):
                raise ValueError("review_budget_configuration_missing")
            logger.info("审查任务开始 review_id=%s provider=%s model=%s retry_count=%s",
                        task.id, task.configuration.provider, task.configuration.model, task.retry_count)
            provider = copy.copy(self._provider)
            if (provider._base_url != task.configuration.endpoint
                    or provider._provider_name != task.configuration.provider):
                raise ValueError("review_provider_changed")
            model = provider.agent_model(task.configuration.model, output_tokens=task.configuration.limits.output_tokens)
            saver = await get_checkpointer() if self._repository else None
            graph = build_review_graph(saver)
            config = {**review_thread_config(task_id), "max_concurrency": task.configuration.limits.concurrency,
                      "recursion_limit": 40}
            async def synthesis_facts(report):
                from app.review.observations import validated_observations
                from app.review.synthesis import synthesis_materials
                from app.services.publication.publication_planner import PublicationPlanner

                def prepare_facts():
                    projected = task.model_copy(deep=True)
                    projected.review_report = report
                    projected.status = {"completed": "completed", "failed": "failed"}.get(
                        report.termination, "completed_with_warnings")
                    plan = PublicationPlanner(self._artifacts).build(projected, include_markdown=False)
                    from app.review.evidence import EvidenceArchive
                    archive = EvidenceArchive(snapshot=report.snapshot, artifacts=self._artifacts)
                    observations = validated_observations(report, archive)
                    return synthesis_materials(report, plan, observations)

                return await asyncio.to_thread(prepare_facts)

            context = ReviewContext(load=load, model=model, limits=task.configuration.limits,
                depth=task.configuration.options.depth, focus=tuple(task.configuration.options.focus),
                progress=progress, check_active=active,
                synthesis_facts=synthesis_facts,
                save_report=persist_report, check_running=running, remaining_seconds=time_left,
                previous_synthesis=(task.review_report.synthesis.model_copy(deep=True)
                    if task.review_report and task.review_report.synthesis else None),
                selected_paths=selected_paths,
                confirmed_plan=plan_review(
                    [item.model_dump(mode="json") for item in task.changed_files],
                    depth=task.configuration.options.depth, selected_paths=selected_paths,
                    focus=task.configuration.options.focus, limits=task.configuration.limits))
            context.budget = ReviewBudget(task.configuration.limits, repository=self._repository,
                task_id=task.id, lease=lease, active=active, depth=task.configuration.options.depth)
            state = await graph.aget_state(config) if saver else None
            if state and state.values and self._repository:
                observed = (state.values.get("discovery_results", []) + state.values.get("verification_results", [])
                            + state.values.get("investigation_results", []) + state.values.get("limitation_results", []))
                budget = await context.budget.summary()
                if observed and any(item.get("model_calls") != 0 for item in observed) and not budget["model_calls"]:
                    raise ValueError("review_budget_history_missing")
            if task.retry_count and (not state or not state.values):
                raise ValueError("review_checkpoint_not_resumable")
            retry_started = False
            if (state and state.values and task.retry_count > state.values.get("retry_count", 0)):
                await active()
                await graph.aupdate_state(config, retry_updates(state.values, task.configuration.limits),
                                          as_node="plan")
                state = await graph.aget_state(config)
                retry_started = True
            context.retry_results = {(item["reviewer"], item["result"]["id"]): item["result"]
                for item in (state.values.get("retry_results", []) if state and state.values else [])}
            context.replaying_nodes = (frozenset(state.next)
                if state and state.values and not retry_started else frozenset())
            async with asyncio.timeout(max(0, remaining)):
                result = await graph.ainvoke(None if state and state.values else {"review_id": task_id},
                                            config, context=context, durability="sync")
            await running()
            task.review_report = ReviewReport.model_validate(result["report"])
            await finalize_report()
        except (ReviewLeaseLost, ReviewStopped):
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, TimeoutError) and facts_saved:
                await running()
                saved = task.review_report.synthesis
                if saved is None:
                    saved = ReviewSynthesis(material_version=digest(task.review_report.snapshot.model_dump_json()))
                if saved.status != "ready":
                    saved.status, saved.error = "fallback", "synthesis_deadline"
                    for attempt in saved.attempts:
                        if attempt.status == "started":
                            attempt.status, attempt.error = "unknown", "synthesis_deadline"
                task.review_report.synthesis = saved
                if context is not None:
                    task.review_report.budget = await context.budget.summary()
                await finalize_report()
            else:
                task.status, task.phase = "failed", "failed"
                task.error = "review_deadline" if isinstance(exc, TimeoutError) else type(exc).__name__
                logger.error("审查任务失败 review_id=%s code=%s", task.id, task.error)
        finally:
            cancel.set()
            self._repo_paths.pop(task_id, None)
            if repo:
                await asyncio.to_thread(cleanup_workspace, repo, workdir=self._git_tool._workdir)
        for step in task.steps:
            if step.status == "running":
                step.status = "failed"
                step.data["termination"] = task.error or task.status
        task.completed_at = task.updated_at = utcnow()
        logger.info("审查任务结束 review_id=%s status=%s code=%s", task.id, task.status, task.error)
        if self._repository:
            await asyncio.to_thread(self._repository.save_task, task, lease=lease)
        else:
            self._tasks[task_id] = task

    def cancel_task(self, task_id):
        task = self.get_task(task_id)
        if not task or task.status in TERMINAL_STATUSES:
            return False
        if self._repository:
            if not self._repository.cancel_task(task_id):
                return False
        else:
            task.status, task.phase = "cancelled", "cancelled"
            task.completed_at = task.updated_at = utcnow()
        if self._worker:
            for job_id, active_id in list(self._worker._active_task_ids.items()):
                running = self._worker._active.get(job_id)
                if active_id == task_id and running is not None and not running.done():
                    running.get_loop().call_soon_threadsafe(running.cancel)
        if task_id in self._run_tasks:
            running = self._run_tasks[task_id]
            running.get_loop().call_soon_threadsafe(running.cancel)
        return True

    async def resume_review(self, task_id):
        task = await asyncio.to_thread(self.get_task, task_id)
        if task is None:
            raise KeyError(task_id)
        if not self._repository or task.status not in {"failed", "completed_with_warnings"}:
            raise ValueError("review_resume_unavailable")
        if (task.configuration.options.limits.total_model_calls != task.configuration.limits.total_model_calls
                or task.configuration.options.limits.total_input_tokens != task.configuration.limits.total_input_tokens):
            raise ValueError("review_budget_history_missing")
        if (utcnow() - task.created_at).total_seconds() >= task.configuration.limits.timeout_seconds:
            raise ValueError("review_deadline")
        state = await build_review_graph(await get_checkpointer()).aget_state(review_thread_config(task_id))
        if not state.next and not state.values.get("report"):
            raise ValueError("review_checkpoint_not_resumable")
        retry_failed = bool(not state.next and state.values.get("report")
                            and state.values["report"]["termination"] in {"partial", "failed"})
        if retry_failed:
            if task.retry_count:
                raise ValueError("review_retry_limit")
            retry_updates(state.values, task.configuration.limits)
            budget = await asyncio.to_thread(self._repository.model_budget_summary,
                                             task.id, task.configuration.limits)
            if (budget["model_calls"] >= task.configuration.limits.total_model_calls
                    or budget["committed_input_tokens"] >= task.configuration.limits.total_input_tokens):
                raise ValueError("review_retry_budget_exhausted")
        elif task.status != "failed":
            raise ValueError("review_resume_unavailable")
        await asyncio.to_thread(self._repository.enqueue_resume, task_id, task.updated_at,
                                f"resume:{uuid4().hex}", retry_failed=retry_failed)
        self.start_worker()
        return await asyncio.to_thread(self.get_task, task_id)

    def delete_task(self, task_id):
        if not self._repository:
            raise ValueError("review_delete_persistence_required")
        return self._repository.delete_task(task_id)
