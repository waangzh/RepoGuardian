"""Fixed input preparation and application lifecycle around ReviewOrchestrator."""

import asyncio
import logging
import threading
from datetime import timedelta
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from app.review.agent import AgentRunner
from app.review.configuration import freeze_configuration
from app.review.context import ContextProvider
from app.review.domain import Overview, Run, Snapshot, TERMINAL, utcnow
from app.review.evidence import EvidenceService
from app.review.index import OnDemandIndex
from app.review.incremental import refine_plan
from app.review.orchestrator import ReviewOrchestrator
from app.review.planner import identity, plan_review
from app.review.report import build_report, generate_overview
from app.review.budget import BudgetGate
from app.review.store import LeaseLost, ReviewStopped
from app.review.tool_scope import is_sensitive_repository_change, is_sensitive_repository_path
from app.services.review.artifact_store import LocalArtifactStore
from app.services.review.task_queue import DatabaseTaskQueue, NonRetryableJobError, ReviewWorker
from app.services.workspaces.prepared_workspaces import (
    PreparedWorkspaces, prepare_repository, prepared_workspace_key,
)
from app.tools.workspace_cleanup import cleanup_workspace

logger = logging.getLogger("RepoGuardian.Review.Service")


class ReviewService:
    def __init__(self, github_tool, git_tool, diff_parser, provider, store, artifacts=None):
        self._github_tool, self._git_tool = github_tool, git_tool
        self._diff_parser, self._provider, self.store = diff_parser, provider, store
        self._artifacts = artifacts or LocalArtifactStore()
        self._task_queue = DatabaseTaskQueue(store.sessions)
        self._worker = ReviewWorker(self._task_queue, self._handle_job, kinds=("review", "overview"))
        self._worker_task = None
        self._repo_paths = {}

    @property
    def repository(self):
        return self.store

    @property
    def github_tool(self):
        return self._github_tool

    def reload_provider(self, provider):
        self._provider = provider

    def get_task(self, task_id):
        run = self.store.get(task_id)
        if run and run.status == "cancelled":
            report = build_report(run, self.store.work_items(run.id), self.store.candidates(run.id),
                                  self.store.usage(run.id))
            report.status, report.overview = "cancelled", Overview(status="skipped_cancelled")
            run.report = report
        return run

    def list_tasks(self, **kwargs):
        return self.store.list_runs(**kwargs)

    def finding_dispositions(self, run_id):
        return self.store.dispositions(run_id)

    def save_finding_disposition(self, run_id, finding_id, value):
        return self.store.save_disposition(run_id, finding_id, value)

    def active_workspace_paths(self):
        return tuple(self._repo_paths.values())

    def worker_running(self):
        return self._worker_task is not None and not self._worker_task.done()

    def start_worker(self):
        if not self.worker_running():
            self._worker_task = asyncio.create_task(self._worker.run_forever())

    async def close(self):
        self._worker.stop()
        for task in list(self._worker._active.values()):
            task.cancel()
        if self._worker_task:
            await self._worker_task

    async def _prepare(self, pr, identity, cancel):
        started = perf_counter()
        logger.info("固定源码准备开始 review_id=%s", identity)
        repo, diff = await asyncio.to_thread(prepare_repository, pr, self._git_tool, self.store,
                                             cancel_event=cancel)
        self._repo_paths[identity] = Path(repo)
        try:
            changed = await asyncio.to_thread(self._diff_parser.parse, diff)
            comparison = await asyncio.to_thread(self._git_tool.review_diff_context,
                                                  repo, pr.base.sha, pr.head.sha)
            snapshot = Snapshot(review_id=identity, repository=f"{pr.owner}/{pr.repo}",
                repository_id=pr.repository_id, pr_number=pr.number,
                declared_base_sha=pr.base.sha, base_sha=comparison["effective_base_sha"], head_sha=pr.head.sha)
            logger.info("固定源码准备完成 review_id=%s changed_files=%d elapsed_ms=%.0f",
                        identity, len(changed), (perf_counter() - started) * 1000)
            return Path(repo), changed, snapshot
        except BaseException:
            await asyncio.to_thread(cleanup_workspace, Path(repo), workdir=self._git_tool._workdir)
            self._repo_paths.pop(identity, None)
            raise

    async def _inputs(self, request):
        config = freeze_configuration(request, self._provider)
        observed_at = utcnow()
        cancel = threading.Event()
        run_id, repo = uuid4().hex, None
        logger.info("审查准备开始 review_id=%s depth=%s", run_id, config.options.depth)
        try:
            pr = await self._github_tool.fetch_pr(str(request.pr_url))
            repo, changed, snapshot = await self._prepare(pr, run_id, cancel)
            plan = await asyncio.to_thread(plan_review, changed, snapshot, config.options)
            now = utcnow()
            seconds = config.options.limits.timeout_seconds
            run = Run(id=run_id, pr_url=str(request.pr_url), pr=pr, snapshot=snapshot,
                configuration=config, changed_files=changed, plan=plan,
                created_at=observed_at,
                review_cutoff=now + timedelta(seconds=seconds * 0.8),
                hard_deadline=now + timedelta(seconds=seconds))
            previous = await asyncio.to_thread(self.store.previous, snapshot)
            history = await asyncio.to_thread(self.store.history, previous)
            old_work = await asyncio.to_thread(self.store.work_items, previous.id) if previous else []
            await asyncio.to_thread(refine_plan, run, previous, old_work, self._git_tool, repo, history)
            logger.info("审查规划完成 review_id=%s units=%d excluded=%d reused_units=%d history_candidates=%d",
                        run_id, len(run.plan.units), len(run.plan.excluded),
                        sum(unit.reused_from is not None for unit in run.plan.units), len(run.plan.history))
            # Publish only the disposable prepared repository, never target changes.
            await asyncio.to_thread(PreparedWorkspaces(self.store, self._git_tool._workdir).publish,
                prepared_workspace_key(pr), repo, pr, {"effective_base_sha": snapshot.base_sha},
                cancel_event=cancel)
            return run
        except asyncio.CancelledError:
            cancel.set()
            logger.info("审查准备取消 review_id=%s", run_id)
            raise
        except Exception as exc:
            logger.warning("审查准备失败 review_id=%s error_type=%s", run_id, type(exc).__name__)
            raise
        finally:
            self._repo_paths.pop(run_id, None)
            if repo:
                await asyncio.to_thread(cleanup_workspace, repo, workdir=self._git_tool._workdir)

    async def preview(self, request):
        run = await self._inputs(request)
        return {"snapshot": run.snapshot, "plan": run.plan, "changed_files": run.changed_files}

    async def create(self, request, key, *, caller_id="installation", expected_snapshot=None):
        normalized = request.model_dump(mode="json")
        if normalized["paths"] is not None:
            normalized["paths"] = sorted(set(normalized["paths"]))
        request_hash = identity(normalized)
        existing = await asyncio.to_thread(self.store.find_intent, caller_id, key, request_hash)
        if existing:
            saved = await asyncio.to_thread(self.get_task, existing)
            if saved is None:
                raise ValueError("review_deleted")
            logger.info("审查创建复用幂等记录 review_id=%s", existing)
            return saved
        run = await self._inputs(request)
        if expected_snapshot and expected_snapshot != (run.snapshot.declared_base_sha, run.snapshot.head_sha):
            raise ValueError("webhook_snapshot_changed")
        result_id = await asyncio.to_thread(self.store.create, run, caller_id, key, request_hash)
        logger.info("审查已入队 review_id=%s", result_id)
        self.start_worker()
        return await asyncio.to_thread(self.get_task, result_id)

    async def _handle_job(self, job):
        lease = {"job_id": job.id, "owner": job.lease_owner, "attempt": job.attempts}
        run = await asyncio.to_thread(self.store.get, job.task_id)
        if run is None or run.status in TERMINAL:
            return
        cancel, repo, index = threading.Event(), None, None
        logger.info("审查作业开始 review_id=%s job_id=%s kind=%s attempt=%d",
                    run.id, job.id, job.kind, job.attempts)
        try:
            if (run.configuration.provider != self._provider._provider_name
                    or run.configuration.endpoint != self._provider._base_url):
                raise NonRetryableJobError("review_provider_changed")
            repo, changed, snapshot = await self._prepare(run.pr, run.id, cancel)
            if snapshot != run.snapshot or changed != run.changed_files:
                raise NonRetryableJobError("fixed_snapshot_inputs_changed")
            paths = await asyncio.to_thread(self._git_tool.list_tracked_files, repo)
            denied = {path for change in changed if is_sensitive_repository_change(
                change.file_path, change.old_file_path)
                for path in (change.file_path, change.old_file_path) if path}
            paths.update(path for change in changed for path in (
                change.file_path, change.old_file_path) if path)
            paths = frozenset(path for path in paths if path not in denied and not is_sensitive_repository_path(path))
            evidence = EvidenceService(snapshot=run.snapshot, artifacts=self._artifacts, repo_path=repo,
                                       allowed_paths=paths, git=self._git_tool)
            index = OnDemandIndex(evidence)
            logger.info("审查上下文就绪 review_id=%s authorized_files=%d denied_files=%d",
                        run.id, len(paths), len(denied))
            provider = ContextProvider(run, evidence, self.store, lease, frozenset(denied), index=index)
            if job.kind == "review":
                await self._seed_history(provider)
            if run.configuration.options.include_pr_discussions:
                provider.discussions = await self._discussions(run, lease)
            model = self._provider.agent_model(run.configuration.model,
                output_tokens=run.configuration.options.limits.output_tokens)
            if job.kind == "overview":
                run = await asyncio.to_thread(self.store.begin, run.id, lease)
                provider.run = run
                report = run.report.model_copy(deep=True)
                report.overview = await generate_overview(report, provider, BudgetGate(self.store, run, lease), model)
                report.version = identity([report.version, report.overview.model_dump()])
                report.usage = await asyncio.to_thread(self.store.usage, run.id)
                await asyncio.to_thread(self.store.save_report, run.id, report, lease, finished=True)
            else:
                await ReviewOrchestrator(self.store, AgentRunner(model)).run(run.id, provider, lease)
        except (ReviewStopped, LeaseLost) as exc:
            logger.info("审查作业停止 review_id=%s job_id=%s reason=%s",
                        run.id, job.id, type(exc).__name__)
            return
        except asyncio.CancelledError:
            cancel.set()
            logger.info("审查作业取消 review_id=%s job_id=%s", run.id, job.id)
            raise
        except Exception as exc:
            logger.error("审查作业失败 review_id=%s job_id=%s kind=%s error_type=%s",
                         run.id, job.id, job.kind, type(exc).__name__)
            await asyncio.to_thread(self.store.fail_run, run.id, "review_execution_failed", lease)
            raise NonRetryableJobError("review_execution_failed") from None
        finally:
            cancel.set()
            if index:
                index.cancel_event.set()
            self._repo_paths.pop(run.id, None)
            if repo:
                await asyncio.to_thread(cleanup_workspace, repo, workdir=self._git_tool._workdir)

    async def _discussions(self, run, lease):
        import json
        from app.tools.github.github_discussions import GitHubDiscussionReader
        if run.discussion_uri:
            frozen = json.loads(await asyncio.to_thread(self._artifacts.read_text, run.discussion_uri))
        else:
            result = await GitHubDiscussionReader(self._github_tool).collect(task_id=run.id,
                pr_url=run.pr_url, head_sha=run.snapshot.head_sha, base_sha=run.snapshot.base_sha,
                owner=run.pr.owner, repo=run.pr.repo, number=run.pr.number)
            artifact = await asyncio.to_thread(self._artifacts.put_text, task_id=run.id,
                                               kind="discussions", content=result.model_dump_json())
            await asyncio.to_thread(self.store.save_discussions, run.id, artifact.uri, lease)
            frozen = result.model_dump(mode="json")
        if frozen["head_sha"] != run.snapshot.head_sha or frozen["base_sha"] != run.snapshot.base_sha:
            raise ValueError("discussion_snapshot_mismatch")
        return frozen["threads"]

    async def _seed_history(self, provider):
        from app.review.domain import Candidate, CandidateInput, SourceRef
        for old in provider.run.plan.history:
            unit = next((unit for unit in provider.run.plan.units if any(
                source["path"] in unit.paths for source in old["sources"])), None)
            if unit is None:
                continue
            sources = []
            for source in old["sources"]:
                try:
                    result = await provider.evidence.read(source["path"], source["start_line"],
                                                          source["end_line"], source["side"])
                except (ValueError, OSError):
                    continue
                if result["evidence"]["content_hash"] != source["content_hash"]:
                    continue  # Old line numbers are hints until content can be located again.
                sources.append(SourceRef.model_validate(result["evidence"]))
            if not sources:
                continue
            candidate = Candidate(id=identity([provider.run.id, old["candidate_id"]])[:32],
                unit_id=unit.id, attempt_id=provider.run.id, tool_call_id=f"history:{old['candidate_id']}",
                previous_candidate_id=old["candidate_id"],
                proposal=CandidateInput(claim=old["claim"], trigger=old["trigger"], impact=old["impact"],
                    source_refs=[ref.id for ref in sources]), sources=sources)
            await asyncio.to_thread(self.store.seed_history, provider.run.id, candidate, provider.lease)

    async def cancel_task(self, run_id):
        cancelled = await asyncio.to_thread(self.store.cancel, run_id)
        if cancelled:
            self._worker.cancel_task(run_id)
            logger.info("审查取消已受理 review_id=%s", run_id)
        return cancelled

    async def retry(self, run_id, *, overview=False):
        await asyncio.to_thread(self.store.retry, run_id, overview=overview)
        logger.info("审查重试已入队 review_id=%s overview_only=%s", run_id, overview)
        self.start_worker()
        return await asyncio.to_thread(self.get_task, run_id)

    def delete_task(self, run_id):
        deleted = self.store.delete_task(run_id)
        if deleted:
            self._artifacts.remove_owner(run_id)
        return deleted

    async def stop_obsolete(self, parsed):
        observed_at = utcnow()
        live = await self._github_tool.fetch_pr(parsed["pr_url"])
        if (live.base.sha, live.head.sha) != (parsed["base_sha"], parsed["head_sha"]):
            return
        stopped = await asyncio.to_thread(self.store.supersede, parsed["repository"],
            live.repository_id, live.number, live.head.sha, observed_at)
        for run_id in stopped:
            self._worker.cancel_task(run_id)
