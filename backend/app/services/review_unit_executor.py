"""Review Unit 独立执行与有界并发调度。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.providers import LLMProvider
from app.services.review_input_context import (
    build_context_evidence,
    build_pr_intent,
    build_working_memory,
    complete_line_prefix,
    make_evidence,
)
from app.review.input_protocol import CANONICAL_UNIT_INPUT_PROTOCOL, legacy_unit_input_allowed
from app.review.unit_completion import DIAGNOSIS_BACKGROUND_DEGRADED
from app.services.fingerprints import unit_execution_fingerprint
from app.review.diff_base import review_diff_base
from app.models.review import (
    AgentAction,
    AgentActionName,
    AgentEvent,
    ChangedFile,
    ContextRetrievalPlan,
    CodeSearchRequest,
    ContextSnippet,
    ExecutionBudget,
    ModelUsage,
    FileFindRequest,
    FileReadDiffRequest,
    FileReadRequest,
    PullRequestInfo,
    ReviewIssue,
    ReviewPhase,
    ReviewToolScope,
    ReviewUnit,
    ReviewUnitComplexity,
    ReviewUnitResult,
    ReviewUnitStatus,
    ReviewUnitTerminalReason,
    ReviewUnitToolEvent,
    UnitPlanStatus,
    UnitReviewPlan,
    UnitReviewSummary,
    UnitRiskHypothesis,
    UnitInputCoverage,
    UnitContextOmission,
    UnitDiffBatch,
    UnitReviewRecord,
    UnitDiffManifest,
    UnitCoverageLedger,
    UnitActiveEvidenceSet,
    UnitEvidenceStore,
    UnitContextSelection,
    UnitBudgetRejection,
)
from app.services.evidence_store import (ContextAdmission, evidence_snapshot, restore_store, put_chunks,
    store_chunks, evidence_priorities, archived_request_chunks, merge_stores, store_catalog, tool_result_metadata)
from app.services.unit_coverage import (build_diff_manifest, new_coverage_ledger, active_evidence_set,
    update_coverage, rebuild_coverage, coverage_gaps, active_body_chars, MAX_ACTIVE_HUNKS, MAX_ACTIVE_DIFF_CHARS,
    unsupported_hunk_ids, NO_HUNK_CHANGE_REASON, BATCH_NO_EVIDENCE_PROGRESS,
    TARGET_HUNK_UNCOVERED, CONTRACT_DEPENDENCY_UNRESOLVED, EVIDENCE_NOT_CARRIED,
    contract_dependency_key)
from app.services.unit_coverage import classified_coverage_gaps, contract_coverage_state
from app.services.review_planner import DeterministicReviewPlanner
from app.services.unit_review_summary import build_record_input, merge_review_summaries, validate_record
from app.tools.code_search import CodeSearchTool
from app.tools.context_files import ScopedContextTool
from app.graph.checkpointer import unit_thread_config
from app.graph.policies import UNIT_ACTION_ROUTES, UNIT_ALLOWED_ACTIONS
from app.review.language_rules import (
    build_language_context,
    markdown_language_for_path,
    render_language_rule_context,
)
from app.review.tool_scope import is_sensitive_repository_change


class _ReviewUnitGraphState(TypedDict, total=False):
    """单个 Review Unit 子图的隔离状态。"""

    parent_state: dict[str, Any]
    unit: ReviewUnit
    scope: ReviewToolScope
    unit_files: list[ChangedFile]
    unit_diff: str
    budget: ExecutionBudget
    skip_plan: bool
    unit_plan: UnitReviewPlan | None
    plan_status: UnitPlanStatus
    plan_skip_reason: str | None
    plan_error: str | None
    context: list[dict[str, Any]]
    issues: list[ReviewIssue]
    pending_issues: list[ReviewIssue]
    model_usages: list[dict[str, Any]]
    messages: list[AgentEvent]
    tool_events: list[ReviewUnitToolEvent]
    retrieval_history: list[dict[str, Any]]
    retrieval_no_new_rounds: int
    issue_round_completed: bool
    legacy_review_action: bool
    next_action: AgentAction | None
    done: bool
    error: str | None
    budget_rejection: UnitBudgetRejection | None
    terminal_reason: ReviewUnitTerminalReason | None
    review_summary: UnitReviewSummary
    last_valid_review_summary: UnitReviewSummary | None
    latest_review_attempt: dict[str, Any]
    input_coverage: UnitInputCoverage | None
    batch_decision_disabled: bool
    diff_manifest: UnitDiffManifest
    coverage_ledger: UnitCoverageLedger
    active_evidence_set: UnitActiveEvidenceSet
    evidence_store: UnitEvidenceStore
    context_selection: UnitContextSelection
    native_review_counts: dict[str, int]
    last_review_evidence_key: str
    last_review_requires_context: bool
    output_repair_attempted: bool


class ReviewUnitExecutor:
    """使用固定数量 worker 执行 Unit，不按 Unit 数量无限创建任务。"""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        concurrency: int,
        timeout_seconds: int,
        planner: DeterministicReviewPlanner | None = None,
        checkpointer: Any | None = None,
        input_mode: str | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("review unit concurrency must be positive")
        if timeout_seconds < 1:
            raise ValueError("review unit timeout must be positive")
        self.provider = provider
        from app.core.config import settings

        self.input_mode = input_mode or settings.repoguardian_unit_input_mode
        if self.input_mode not in {"canonical", "legacy"}:
            raise ValueError("Unit input mode must be canonical or legacy")
        self.concurrency = concurrency
        self.timeout_seconds = timeout_seconds
        self.planner = planner or DeterministicReviewPlanner()
        self.unit_graph = self._build_unit_graph().compile(checkpointer=checkpointer)

    def _supports_native_review_step(self) -> bool:
        from app.agents.providers import OpenAICompatibleProvider
        if not getattr(self.provider, "supports_review_step", False):
            return False
        # A compatibility adapter overriding review_unit alone must retain that
        # contract. Other subclasses inherit the native review_step normally.
        provider_type = type(self.provider)
        return not (getattr(provider_type, "review_step", None) is OpenAICompatibleProvider.review_step
                    and getattr(provider_type, "review_unit", None) is not OpenAICompatibleProvider.review_unit)

    async def execute(
        self,
        units: list[ReviewUnit],
        state: dict[str, Any],
    ) -> list[ReviewUnitResult]:
        if not units:
            return []
        from app.services.review_runtime import ReviewRuntime, ReviewTask, ReviewTaskStatus
        from app.services.review_run_budget import current_run_budget

        run_budget = current_run_budget()
        runtime = ReviewRuntime()

        def deferrals(result):
            batches = [item for item in result.diff_batches if item.admission.get("deferred_budget")]
            items = [(item.reason or "budget_deferred", item.admission.get("budget_revision", run_budget.revision))
                     for item in batches] if run_budget is not None else []
            rejection = result.budget_rejection
            if rejection is not None and rejection.can_defer:
                items.append((rejection.reason, rejection.budget_revision))
            return items

        def defer_unit(index):
            items = deferrals(results[index])
            if run_budget is not None and items:
                runtime.defer(ReviewTask(units[index].id, "review_step", priority=(index,), payload=index),
                    reason=items[0][0], budget_revision=min(item[1] for item in items))
        queue: asyncio.Queue[tuple[int, ReviewUnit] | None] = asyncio.Queue()
        results: list[ReviewUnitResult | None] = [None] * len(units)
        for index, unit in enumerate(units):
            queue.put_nowait((index, unit))
        worker_count = min(self.concurrency, len(units))
        for _ in range(worker_count):
            queue.put_nowait(None)

        async def worker() -> None:
            while True:
                entry = await queue.get()
                try:
                    if entry is None:
                        return
                    index, unit = entry
                    results[index] = await self.execute_unit(unit, state)
                    defer_unit(index)
                finally:
                    queue.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
        try:
            await asyncio.gather(*workers)
        except asyncio.CancelledError:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise
        if run_budget is not None:
            async def execute_deferred(task):
                index = task.payload
                refreshed_state = {**state, "review_unit_results": [
                    result.model_dump(mode="json") for result in results if result is not None]}
                results[index] = await self.execute_unit(units[index], refreshed_state)
                if deferrals(results[index]):
                    defer_unit(index)
                    task.status = ReviewTaskStatus.DEFERRED_BUDGET
            for _ in range(len(units)):
                revision = run_budget.revision
                await runtime.deferred.drain(budget_revision=revision,
                    admit=lambda _task: {"admitted": True}, execute=execute_deferred)
                if revision == run_budget.revision:
                    break
        return [result for result in results if result is not None]

    async def execute_unit(
        self,
        unit: ReviewUnit,
        state: dict[str, Any],
    ) -> ReviewUnitResult:
        from app.services.model_request_budgeter import unit_budget_snapshot, estimate_tokens

        await asyncio.to_thread(estimate_tokens, "", state.get("model") or getattr(self.provider, "_default_model", ""))
        snapshot = {"budget": self._budget_for(unit), "input_fingerprint": unit_execution_fingerprint(
            unit.fingerprint, state, self.provider, self.input_mode)}
        snapshot_token = unit_budget_snapshot.set(snapshot)
        try:
            self._check_provider_protocol()
            async with asyncio.timeout(self.timeout_seconds):
                result = await self._execute_unit(unit, state)
                from app.review.unit_completion import is_review_unit_complete

                return result.model_copy(update={
                    "unit_completion_status": (
                        "complete"
                        if is_review_unit_complete(result)
                        else result.unit_completion_status
                        if result.unit_completion_status == "complete_with_unresolved_contracts"
                        else "incomplete"
                    )
                })
        except TimeoutError:
            if snapshot.get("diff_batches"):
                return self._aggregate_diff_batches(unit, snapshot["diff_batches"], snapshot["budget"],
                    snapshot["input_fingerprint"], reason="review_unit_timed_out", manifest=snapshot.get("diff_manifest"), evidence_store=snapshot.get("evidence_store"))
            return ReviewUnitResult(
                review_unit_id=unit.id,
                input_fingerprint=snapshot["input_fingerprint"],
                output_repair_attempted=snapshot.get("output_repair_attempted", False), issues=snapshot.get("issues") or [],
                status=ReviewUnitStatus.timed_out,
                terminal_reason=ReviewUnitTerminalReason.timed_out,
                plan_skipped=False,
                execution_budget=snapshot["budget"],
                error=f"review unit timed out after {self.timeout_seconds} seconds",
                review_summary=self._failure_summary(snapshot, "review_unit_timed_out"),
                context_snippets=snapshot.get("context") or [],
                plan=snapshot.get("unit_plan"),
                plan_status=snapshot.get("plan_status") or UnitPlanStatus.skipped,
                input_coverage=snapshot.get("input_coverage"),
                model_usages=snapshot.get("model_usages") or [],
                diff_manifest=snapshot.get("diff_manifest"), coverage_ledger=snapshot.get("coverage_ledger"),
                active_evidence_set=snapshot.get("active_evidence_set"),
                evidence_store=snapshot.get("evidence_store"), context_selection=snapshot.get("context_selection"),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            from app.agents.providers import LLMProviderError
            from app.services.model_usage import annotate_usage

            if snapshot.get("diff_batches"):
                return self._aggregate_diff_batches(unit, snapshot["diff_batches"], snapshot["budget"],
                    snapshot["input_fingerprint"], reason=f"review_unit_failed:{type(exc).__name__}", manifest=snapshot.get("diff_manifest"), evidence_store=snapshot.get("evidence_store"))
            usage = exc.usage if isinstance(exc, LLMProviderError) else None
            usage = annotate_usage(
                usage,
                review_unit_id=unit.id,
                unit_complexity=unit.complexity,
            )
            return ReviewUnitResult(
                review_unit_id=unit.id,
                input_fingerprint=snapshot["input_fingerprint"],
                output_repair_attempted=snapshot.get("output_repair_attempted", False), issues=snapshot.get("issues") or [],
                status=ReviewUnitStatus.failed,
                terminal_reason=(
                    ReviewUnitTerminalReason.provider_error
                    if isinstance(exc, LLMProviderError)
                    else ReviewUnitTerminalReason.execution_error
                ),
                plan_skipped=False,
                execution_budget=getattr(exc, "execution_budget", snapshot["budget"]),
                model_usages=[*snapshot.get("model_usages", []), *([usage] if usage is not None else [])],
                error=f"{type(exc).__name__}: {exc}",
                review_summary=self._failure_summary(snapshot, "review_unit_failed"),
                context_snippets=snapshot.get("context") or [],
                plan=snapshot.get("unit_plan"),
                plan_status=snapshot.get("plan_status") or UnitPlanStatus.skipped,
                input_coverage=snapshot.get("input_coverage"),
                diff_manifest=snapshot.get("diff_manifest"), coverage_ledger=snapshot.get("coverage_ledger"),
                active_evidence_set=snapshot.get("active_evidence_set"),
                evidence_store=snapshot.get("evidence_store"), context_selection=snapshot.get("context_selection"),
            )
        finally:
            unit_budget_snapshot.reset(snapshot_token)

    async def _execute_unit(
        self,
        unit: ReviewUnit,
        state: dict[str, Any],
    ) -> ReviewUnitResult:
        all_changed = [
            ChangedFile.model_validate(item)
            for item in state.get("changed_files") or []
        ]
        primary_paths = set(unit.primary_files)
        sensitive_files = [
            item.old_file_path or item.file_path
            for item in all_changed
            if item.file_path in primary_paths
            and is_sensitive_repository_change(item.file_path, item.old_file_path)
        ]
        if sensitive_files:
            raise ValueError(
                f"sensitive changed files cannot enter a Review Unit: {sensitive_files}"
            )
        by_path = {item.file_path: item for item in all_changed}
        unit_files = self._unit_changed_files(unit, by_path)
        if self.input_mode == "canonical":
            from app.services.file_change_evidence import bind_file_change_evidence
            from app.tools.git_tool import GitTool

            unit_files = await asyncio.to_thread(bind_file_change_evidence, unit_files,
                state.get("repo_path"), str(state.get("head_sha") or ""), review_diff_base(state),
                state.get("_git_tool") or GitTool())
            by_path.update({file.file_path: file for file in unit_files})
            state = {**state, "changed_files": [by_path[file.file_path].model_dump(mode="json") for file in all_changed]}
        repository_files = {
            str(item["path"])
            for item in state.get("file_index") or []
            if isinstance(item.get("path"), str)
        }
        scope = self.planner.build_scope(
            unit,
            state.get("repo_path") or None,
            repository_files=repository_files,
        )
        budget = self._budget_for(unit)
        skip_plan = self.planner.should_skip_plan(unit, all_changed)
        batch = state.get("_unit_diff_batch")
        if self.input_mode == "canonical":
            manifest = (UnitDiffManifest.model_validate(state["diff_manifest"]) if batch else
                        build_diff_manifest(unit, unit_files, str(state.get("head_sha") or ""), review_diff_base(state)))
            ledger = (UnitCoverageLedger.model_validate(state["coverage_ledger"]) if batch else new_coverage_ledger(manifest))
            state = {**state, "diff_manifest": manifest.model_dump(mode="json"),
                     "coverage_ledger": ledger.model_dump(mode="json"),
                     "unit_metadata": state.get("unit_metadata") or unit.model_dump(mode="json")}
        if batch:
            budget = batch["budget"]
            skip_plan = batch.get("skip_plan", True)
        elif self.input_mode == "canonical" and not state.get("cross_unit_followup") and any(file.hunks or file.file_change_evidence for file in unit_files):
            admission = self._workset_admission(unit, unit_files, state, budget)
            if admission.get("can_defer"):
                return await self._execute_diff_batches(unit, unit_files, state, budget)
            minimum = ([unit_files[0].model_copy(update={"hunks": []})]
                       if not admission["admitted"] else None)
            minimum_unit = unit.model_copy(update={"primary_files": [unit.primary_files[0]],
                "diff_hunk_ids": [], "related_files": unit.related_files[:12], "changed_symbols": [],
                "context_provenance": [], "grouping_reason": "bounded_diff_workset"})
            if (admission.get("reason") in {"unit_request_budget_exhausted", "required_input_too_large", "active_hunk_set_exceeded", "model_context_window_exceeded",
                    "model_input_limit_exceeded"} and minimum is not None
                    and self._workset_admission(minimum_unit, minimum, {key: value for key, value in state.items()
                        if key not in {"diff_manifest", "coverage_ledger", "unit_metadata"}}, budget)["admitted"]):
                return await self._execute_diff_batches(unit, unit_files, state, budget)
        followup = state.get("cross_unit_followup")
        followup_plan = None
        if followup:
            # 补查复用只读子图，但禁止发现范围扩张，并跳过二次规划。
            scope = scope.model_copy(update={
                "readable_files": set(unit.primary_files) | set(unit.related_files),
                "repository_discovery_enabled": False,
                "max_context_chars": 12_000,
            })
            budget = ExecutionBudget.model_validate(state["_cross_unit_budget"]) if state.get("_cross_unit_budget") else ExecutionBudget(
                max_model_calls=3, max_token_usage=12_000, max_diagnosis_attempts=2 if self._supports_native_review_step() else 1,
                max_context_retrievals=2, max_patch_attempts=0)
            skip_plan = True
            followup_plan = UnitReviewPlan(
                change_summary=followup["question"],
                review_objectives=[followup["question"], followup["counterevidence_goal"]],
                coverage_targets=[followup["stop_condition"]],
                risk_hypotheses=[UnitRiskHypothesis(
                    id="followup-question", category="correctness", priority="high",
                    description=followup["question"], affected_files=unit.primary_files,
                    evidence_needed=[followup["counterevidence_goal"]],
                    completion_criteria=followup["stop_condition"][:500],
                )],
                initial_action=AgentAction(action=AgentActionName.report_issue,
                                           reason="执行有界定向补查"),
            )
        graph_state: _ReviewUnitGraphState = {
            "parent_state": {key: value for key, value in state.items() if not key.startswith("_")},
            "unit": unit,
            "scope": scope,
            "unit_files": unit_files,
            "unit_diff": self._unit_diff(unit, by_path),
            "budget": budget,
            "skip_plan": skip_plan,
            "unit_plan": followup_plan or (batch.get("plan") if batch else None),
            "plan_status": UnitPlanStatus.skipped if skip_plan else UnitPlanStatus.failed,
            "plan_skip_reason": "small_low_risk_unit" if skip_plan else None,
            "plan_error": None,
            "context": [],
            "issues": [],
            "model_usages": [],
            "messages": [],
            "tool_events": [],
            "retrieval_history": [],
            "retrieval_no_new_rounds": 0,
            "issue_round_completed": False,
            "legacy_review_action": False,
            "done": False,
            "terminal_reason": None,
            "review_summary": UnitReviewSummary(reason=("legacy_provider_without_canonical_evidence_protocol"
                if self.input_mode == "legacy" else "diagnosis_not_executed"), latest_attempt_status="not_executed",
                input_protocol="legacy" if self.input_mode == "legacy" else CANONICAL_UNIT_INPUT_PROTOCOL),
            "last_valid_review_summary": None,
            "latest_review_attempt": {"status": "not_executed"},
            "evidence_store": restore_store(state.get("batch_evidence_store"),
                manifest.review_unit_id if self.input_mode == "canonical" else unit.id,
                evidence_snapshot(str(state.get("head_sha") or ""), review_diff_base(state)), scope.readable_files),
        }
        if batch:
            graph_state.update(pending_issues=[], error=None)
            graph_state["batch_decision_disabled"] = batch.get("skip_decision", False)
            graph_state["next_action"] = AgentAction(action=AgentActionName.report_issue,
                reason="优先诊断当前完整 Diff 工作集，保留其他批次的诊断预算")
            if not skip_plan:
                graph_state["next_action"] = None
        if self.input_mode == "canonical":
            graph_state.update(diff_manifest=manifest, coverage_ledger=ledger,
                active_evidence_set=active_evidence_set(manifest, unit_files, unit.id,
                    (state.get("unit_diff_batch") or {}).get("ranges")))
        from app.services.model_request_budgeter import unit_budget_snapshot

        observer = unit_budget_snapshot.get()
        binding = observer["input_fingerprint"] if observer else unit_execution_fingerprint(
            unit.fingerprint, state, self.provider, self.input_mode)
        prior = next((ReviewUnitResult.model_validate(raw) for raw in state.get("review_unit_results") or []
                      if raw.get("review_unit_id") == unit.id and raw.get("input_fingerprint") == binding), None)
        if prior and self.input_mode == "canonical":
            restored_store = restore_store(prior.evidence_store, manifest.review_unit_id,
                evidence_snapshot(str(state.get("head_sha") or ""), review_diff_base(state)), scope.readable_files)
            previous_chunks, _ = build_context_evidence([item.model_dump(mode="json") for item in prior.context_snippets
                if item.file in scope.readable_files and not is_sensitive_repository_change(item.file)],
                str(state.get("head_sha") or ""), review_diff_base(state))
            if prior.evidence_store is None:
                put_chunks(restored_store, previous_chunks)
            inventory = store_chunks(restored_store)
            restored_context, used = [], 0
            for chunk in previous_chunks:
                identity = chunk["evidence_id"]
                if identity in inventory and used + len(chunk["content"]) <= scope.max_context_chars:
                    restored_context.append(inventory[identity])
                    used += len(chunk["content"])
            restored_plan = followup_plan or prior.plan
            request = build_record_input(unit_files, restored_context, restored_plan,
                str(state.get("head_sha") or ""), review_diff_base(state))
            request["pr_intent"] = build_pr_intent(state.get("pr_info"))
            if self.input_mode == "canonical":
                request["diff_manifest"] = manifest.model_dump(mode="json")
            recovered = merge_review_summaries(prior.review_summary, validate_record(None, request, scope.readable_files), None)
            if recovered.last_valid_record:
                recovered = recovered.model_copy(update={"record": prior.review_summary.record,
                    "status": prior.review_summary.status, "latest_attempt_status": prior.review_summary.latest_attempt_status,
                    "latest_attempt_reason": prior.review_summary.latest_attempt_reason, "reason": prior.review_summary.reason})
            graph_state.update(context=restored_context, unit_plan=restored_plan,
                evidence_store=restored_store, context_selection=UnitContextSelection(
                    revision=restored_store.revision, stored_count=len(restored_store.entries),
                    active_evidence_ids=[item["evidence_id"] for item in restored_context],
                    evicted_evidence_ids=[identity for identity in (prior.context_selection.active_evidence_ids if prior.context_selection else [])
                        if identity not in {item["evidence_id"] for item in restored_context}]),
                skip_plan=True if restored_plan else skip_plan, budget=prior.execution_budget,
                plan_status=prior.plan_status if restored_plan else graph_state["plan_status"],
                plan_skip_reason="restored_plan" if restored_plan else graph_state["plan_skip_reason"],
                review_summary=recovered, last_valid_review_summary=recovered if recovered.last_valid_record else None,
                issues=list(prior.issues), output_repair_attempted=prior.output_repair_attempted,
                issue_round_completed=bool(recovered.record and recovered.status == "reported"),
                latest_review_attempt={"status": recovered.latest_attempt_status or recovered.status, "reason": recovered.reason},
                input_coverage=prior.input_coverage,
                model_usages=[item.model_dump(mode="json") for item in prior.model_usages])
            if self.input_mode == "canonical":
                graph_state["coverage_ledger"] = update_coverage(ledger, manifest,
                    graph_state["active_evidence_set"], recovered, restored_plan)
        if batch and state.get("batch_supporting_context"):
            context, admission = self._admit_retrieved_context(graph_state, state["batch_supporting_context"], graph_state["budget"])
            graph_state.update(**self._context_selection_update(graph_state, admission),
                               input_coverage=self._retrieval_input_coverage(graph_state, admission))
        if followup and not prior:
            seeds = [item for item in state.get("cross_unit_followup_context") or [] if item.get("file") in scope.readable_files]
            context, admission = self._admit_retrieved_context(graph_state, seeds, graph_state["budget"])
            graph_state.update(**self._context_selection_update(graph_state, admission),
                               input_coverage=self._retrieval_input_coverage(graph_state, admission))
            if seeds:
                graph_state["tool_events"].append(ReviewUnitToolEvent(review_unit_id=unit.id,
                    tool="followup_context", status=admission["status"], result_count=len(context),
                    detail=json.dumps(admission, ensure_ascii=False)))
        self._remember_unit_state(graph_state)
        if self.input_mode == "canonical" and unit_files and not any(file.hunks or file.file_change_evidence for file in unit_files):
            coverage = UnitInputCoverage(evidence_coverage="partial", target_coverage="partial",
                reason=NO_HUNK_CHANGE_REASON, omitted_components=["diff_evidence"],
                omitted_targets=unsupported_hunk_ids(manifest, graph_state["active_evidence_set"]))
            summary = graph_state["review_summary"].model_copy(update={"status": "unknown", "record": None,
                "reason": NO_HUNK_CHANGE_REASON, "latest_attempt_status": "not_executed",
                "latest_attempt_reason": NO_HUNK_CHANGE_REASON})
            self._remember_unit_state({**graph_state, "input_coverage": coverage, "review_summary": summary})
            return ReviewUnitResult(review_unit_id=unit.id, input_fingerprint=binding,
                status=ReviewUnitStatus.failed, terminal_reason=ReviewUnitTerminalReason.unsupported_change,
                error="文件变更没有可审查的文本 Hunk，当前证据协议不支持仅凭文件元数据完成检查；需人工复核。",
                plan_skipped=True, plan_status=UnitPlanStatus.skipped, plan_skip_reason=NO_HUNK_CHANGE_REASON,
                input_coverage=coverage, review_summary=summary, execution_budget=graph_state["budget"],
                model_usages=graph_state.get("model_usages") or [],
                context_snippets=graph_state["context"], evidence_store=graph_state.get("evidence_store"),
                context_selection=graph_state.get("context_selection"), diff_manifest=manifest,
                coverage_ledger=graph_state["coverage_ledger"], active_evidence_set=graph_state["active_evidence_set"])
        config = None
        if getattr(self.unit_graph, "checkpointer", None) not in (None, False):
            config = unit_thread_config(str(state.get("task_id") or "unknown"), unit.id)
        result = await self.unit_graph.ainvoke(graph_state, config=config)
        if result.get("done") and not result.get("error"):
            terminal_reason = result.get("terminal_reason") or (
                ReviewUnitTerminalReason.completed
                if result.get("issues")
                else ReviewUnitTerminalReason.no_issue
            )
        else:
            terminal_reason = (
                result.get("terminal_reason")
                or ReviewUnitTerminalReason.execution_error
            )
        return ReviewUnitResult(
            review_unit_id=unit.id,
            input_fingerprint=binding,
            status=(
                ReviewUnitStatus.completed
                if result.get("done") and not result.get("error")
                else ReviewUnitStatus.failed
            ),
            terminal_reason=terminal_reason,
            plan_skipped=result.get("plan_status") == UnitPlanStatus.skipped,
            plan=result.get("unit_plan"),
            plan_status=result.get("plan_status"),
            plan_skip_reason=result.get("plan_skip_reason"),
            plan_error=result.get("plan_error"),
            input_coverage=result.get("input_coverage"),
            review_summary=result.get("review_summary") or UnitReviewSummary(),
            issues=result.get("issues") or [],
            output_repair_attempted=result.get("output_repair_attempted", False),
            context_snippets=[
                ContextSnippet.model_validate(item) for item in result.get("context") or []
            ],
            messages=result.get("messages") or [],
            tool_events=result.get("tool_events") or [],
            execution_budget=result.get("budget") or budget,
            model_usages=result.get("model_usages") or [],
            error=result.get("error"),
            budget_rejection=result.get("budget_rejection"),
            diff_manifest=result.get("diff_manifest"), coverage_ledger=result.get("coverage_ledger"),
            active_evidence_set=result.get("active_evidence_set"),
            evidence_store=result.get("evidence_store"), context_selection=result.get("context_selection"),
        )

    def _workset_admission(self, unit, files, state, budget, metadata=None):
        """发送前检查完整诊断与 Decision；不消费预算或建立传输。"""
        from app.agents.providers import LLMProviderError, OpenAICompatibleProvider
        from app.services.context_assembler import RequiredInputTooLarge
        from app.services.review_admission import AdmissionKind, annotate_admission, budget_fit
        from app.services.review_run_budget import current_run_budget

        if sum(max(1, len(file.hunks)) for file in files) > MAX_ACTIVE_HUNKS or active_body_chars(files) > MAX_ACTIVE_DIFF_CHARS:
            return annotate_admission({"reason": "active_hunk_set_exceeded"}, admitted=False,
                                      reason="active_hunk_set_exceeded", kind=AdmissionKind.REQUEST_FIT,
                                      can_defer=False, can_split=True)

        scope = self.planner.build_scope(unit, state.get("repo_path"), repository_files={
            item["path"] for item in state.get("file_index") or [] if item.get("path")})
        by_path = {file.file_path: file for file in files}
        parent = {**state, "unit_diff_batch": {key: value for key, value in metadata.items()
                  if key not in {"admission", "reason"}} if metadata else None}
        trial = {"parent_state": parent, "unit": unit, "scope": scope, "unit_files": files,
                 "unit_diff": self._unit_diff(unit, by_path), "context": [], "budget": budget,
                 "unit_plan": state.get("batch_plan")}
        if state.get("diff_manifest"):
            trial.update(diff_manifest=UnitDiffManifest.model_validate(state["diff_manifest"]),
                         coverage_ledger=UnitCoverageLedger.model_validate(state["coverage_ledger"]))
        try:
            decision = self._unit_state(parent, unit, scope, files, budget, [], unit_diff=trial["unit_diff"],
                                        unit_plan=trial["unit_plan"])
            decision.update(unit_agent=True, issue_round_completed=False, reported_issue_count=0,
                            retrieval_no_new_rounds=0)
            estimator = getattr(self.provider, "unit_decision_admission", None)
            if not callable(estimator):
                OpenAICompatibleProvider._build_decision_prompt(decision)
            estimate = self._diagnosis_estimate(self._diagnosis_args(trial))
            estimate["decision_admitted"] = True
            if callable(estimator):
                try:
                    decision_estimate = estimator(decision, state.get("model"))
                    estimate["decision_reserved_tokens"] = decision_estimate["reserved_tokens"]
                    estimate["decision_admitted"] = budget.can_consume(model_calls=2,
                        token_usage=estimate["reserved_tokens"] + decision_estimate["reserved_tokens"])
                except LLMProviderError as exc:
                    if str(exc).partition(":")[0] not in {"required_input_too_large", "model_context_window_exceeded",
                            "model_input_limit_exceeded", "model_output_limit_exceeded"}:
                        raise
                    estimate["decision_admitted"] = False
        except (LLMProviderError, RequiredInputTooLarge) as exc:
            reason = str(exc).partition(":")[0]
            if reason not in {"required_input_too_large", "model_context_window_exceeded",
                              "model_input_limit_exceeded", "model_output_limit_exceeded"}:
                raise
            return annotate_admission({}, admitted=False, reason=reason,
                                      kind=AdmissionKind.REQUEST_FIT, can_defer=False, can_split=True)
        admitted = budget.can_consume(model_calls=1, diagnosis_attempts=1,
                                     token_usage=estimate["reserved_tokens"])
        run = current_run_budget()
        if run is not None:
            available = run.available()
            admitted = available["calls"] > 0 and available["tokens"] >= estimate["reserved_tokens"]
        if admitted:
            decision_fit = budget_fit(budget, token_usage=estimate["reserved_tokens"], model_calls=1,
                                      diagnosis_attempts=1)
            return annotate_admission({**estimate}, admitted=True,
                                      budget=decision_fit, kind=AdmissionKind.BUDGET,
                                      can_defer=False, can_split=False)
        decision_fit = budget_fit(budget, token_usage=estimate["reserved_tokens"], model_calls=1,
                                  diagnosis_attempts=1)
        return annotate_admission({**estimate}, admitted=False,
                                  reason="unit_request_budget_exhausted", budget=decision_fit,
                                  kind=AdmissionKind.BUDGET, can_defer=True, can_split=False)

    async def _execute_diff_batches(self, unit, files, state, budget):
        from app.services.unit_worksets import build_unit_worksets
        from app.services.model_request_budgeter import unit_budget_snapshot
        from app.review.unit_completion import (
            is_review_unit_execution_complete,
            is_reusable_review_unit_result,
        )

        manifest = UnitDiffManifest.model_validate(state["diff_manifest"])
        from app.services.review_runtime import ReviewRuntime, ReviewTask
        from app.services.review_run_budget import current_run_budget

        runtime = ReviewRuntime()
        local_revision = 0

        def revision():
            run = current_run_budget()
            return (run.revision if run is not None else 0) + local_revision

        def defer(index, entry):
            entry.admission["deferred_budget"] = True
            if current_run_budget() is not None:
                entry.admission["budget_revision"] = current_run_budget().revision
            runtime.defer(ReviewTask(entry.id, "review_step", priority=(index,), payload=index),
                          reason=entry.reason or "budget_deferred", budget_revision=revision())

        worksets = build_unit_worksets(unit, files,
            lambda child, selected, metadata: self._workset_admission(child, selected, state, budget, metadata))
        entries = [UnitDiffBatch(**metadata) for _, _, metadata in worksets]
        # Allocate one base diagnosis per executable workset, once for the whole parent.
        # Keep the Unit's token/retrieval limits and a fixed shared optional/retry allowance.
        allocated = self._budget_for(unit, workset_count=max(1, sum(
            bool(entry.admission["admitted"]) for entry in entries)))
        budget = budget.model_copy(update={name: getattr(allocated, name) for name in (
            "max_model_calls", "max_diagnosis_attempts")})
        observer = unit_budget_snapshot.get()
        binding = observer["input_fingerprint"] if observer else unit_execution_fingerprint(
            unit.fingerprint, state, self.provider, self.input_mode)
        prior = next((ReviewUnitResult.model_validate(raw) for raw in state.get("review_unit_results") or []
                      if raw.get("review_unit_id") == unit.id and raw.get("input_fingerprint") == binding), None)
        saved = {entry.id: entry for entry in prior.diff_batches} if prior else {}
        if prior and saved:
            budget = budget.model_copy(update={name: getattr(prior.execution_budget, name) for name in (
                "model_calls", "token_usage", "diagnosis_attempts", "context_retrievals", "patch_attempts")})
        for index, (child, selected, _) in enumerate(worksets):
            entry, cached = entries[index], saved.get(entries[index].id)
            if (cached and cached.input_hash == entry.input_hash and cached.result
                    and cached.result.input_fingerprint == unit_execution_fingerprint(
                        child.fingerprint, state, self.provider, self.input_mode)
                    and cached.status == "completed" and is_reusable_review_unit_result(cached.result)):
                request = build_record_input(selected,
                    [snippet.model_dump(mode="json") for snippet in cached.result.context_snippets],
                    cached.result.plan, str(state.get("head_sha") or ""), review_diff_base(state))
                from app.services.file_change_evidence import set_file_change_impact_scope
                set_file_change_impact_scope(request, child.related_files)
                request["pr_intent"] = build_pr_intent(state.get("pr_info"))
                request["diff_manifest"] = state["diff_manifest"]
                expected_active = active_evidence_set(manifest, selected, child.id, entry.ranges,
                    [snippet.model_dump(mode="json") for snippet in cached.result.context_snippets])
                if cached.result.diff_manifest != manifest or cached.result.active_evidence_set != expected_active:
                    continue
                restored_memory = self._batch_memory(entries[:index], rebuild_coverage(manifest, entries[:index]))
                if restored_memory:
                    request["working_memory"] = build_working_memory(restored_memory, request, child.id)[0]
                scope = self.planner.build_scope(child, state.get("repo_path"), repository_files={
                    item["path"] for item in state.get("file_index") or [] if item.get("path")})
                validated = validate_record(cached.result.review_summary.record, request, scope.readable_files)
                trusted = {reference.id: reference for reference in cached.result.review_summary.evidence}
                if (validated.status == "reported" and all(trusted.get(reference.id) == reference
                        for reference in validated.evidence)
                        and cached.result.review_summary.last_valid_snapshot == validated.latest_attempt_snapshot
                        and all(snippet.file in scope.readable_files and not is_sensitive_repository_change(snippet.file)
                                for snippet in cached.result.context_snippets)):
                    entry.result, entry.status, entry.reason = cached.result, "completed", "restored_valid_batch"
        if observer is not None:
            observer["diff_batches"] = entries
            observer["diff_manifest"] = manifest
            observer["budget"] = budget
        async def execute_batch(index, *, draining=False):
            nonlocal budget, local_revision
            child, selected, metadata = worksets[index]
            entry = entries[index]
            if entry.status == "completed":
                return
            if not draining and not entry.admission["admitted"]:
                entry.status = "skipped"
                entry.reason = entry.reason or entry.admission["reason"]
                if entry.admission.get("can_defer"):
                    defer(index, entry)
                return
            future = [] if draining else [item for item in entries[index + 1:]
                if item.admission["admitted"] and item.status != "completed"]
            ledger = rebuild_coverage(manifest, entries)
            shared_plan = next((item.result.plan for item in entries if item.result and item.result.plan), None)
            forecast = {**state, "coverage_ledger": ledger.model_dump(mode="json"), "batch_plan": shared_plan,
                        "batch_memory_summary": self._batch_memory(entries[:index], ledger)}
            current_admission = self._workset_admission(child, selected, forecast, budget, metadata)
            if not current_admission["admitted"]:
                entry.status = "skipped"
                entry.reason = current_admission["reason"]
                entry.admission = current_admission
                if current_admission.get("can_defer"):
                    defer(index, entry)
                return
            # 公共清单/账本/规划增量只计数一次，避免逐批重渲染整个剩余队列。
            growth = max(0, current_admission["reserved_tokens"] - entry.admission.get("reserved_tokens", 0))
            held = self._workset_holdback(budget, future, current_admission["reserved_tokens"], growth)
            entry.admission = {**current_admission, "future_holdback": held}
            local = budget.model_copy(update={
                "max_model_calls": budget.max_model_calls - held["model_calls"],
                "max_diagnosis_attempts": budget.max_diagnosis_attempts - held["diagnosis_attempts"],
                "max_token_usage": budget.max_token_usage - held["token_usage"],
            })
            if not local.can_consume(model_calls=1, diagnosis_attempts=1,
                                    token_usage=entry.admission["reserved_tokens"]):
                entry.status, entry.reason = "skipped", "unit_workset_budget_exhausted"
                defer(index, entry)
                return
            # 未来诊断预留扣除后，再检查本批可选决策与诊断能否同时执行。
            if "decision_reserved_tokens" in entry.admission:
                entry.admission["decision_admitted"] = entry.admission["decision_admitted"] and local.can_consume(
                    model_calls=2, token_usage=entry.admission["reserved_tokens"] + entry.admission["decision_reserved_tokens"])
            batch_state = {**state, "changed_files": [file.model_dump(mode="json") for file in selected],
                "review_unit_results": [], "unit_diff_batch": {key: value for key, value in metadata.items()
                    if key not in {"admission", "reason"}}, "_unit_diff_batch": {
                        "budget": local, "skip_decision": not entry.admission.get("decision_admitted", True),
                        "skip_plan": index != 0 or not entry.admission.get("decision_admitted", True), "plan": shared_plan}}
            batch_state["coverage_ledger"] = ledger.model_dump(mode="json")
            memory = self._batch_memory(entries[:index], ledger)
            if memory:
                batch_state["batch_memory_summary"] = memory
            batch_state["batch_supporting_context"] = [
                snippet.model_dump(mode="json")
                for item in entries[:index]
                if item.result and item.result.review_summary.status == "reported"
                for snippet in item.result.context_snippets
            ]
            prior_support, contract_handoff = self._batch_supporting_context(
                worksets,
                entries,
                index,
                manifest,
                selected,
                str(state.get("head_sha") or ""),
                review_diff_base(state),
            )
            entry.contract_handoff = contract_handoff
            batch_state["batch_supporting_context"].extend(prior_support)
            prior_ids = {
                item["derived_from_evidence_id"]
                for item in prior_support
                if item.get("derived_from_evidence_id")
            }
            batch_state["batch_prior_evidence_bindings"] = [
                reference.model_dump(mode="json")
                for item in entries[:index]
                if item.result
                for reference in item.result.review_summary.evidence
                if reference.source == "diff" and reference.id in prior_ids
            ]
            batch_state["batch_evidence_store"] = merge_stores([item.result.evidence_store for item in entries[:index] if item.result])
            batch_state["batch_review_issues"] = [issue for item in entries[:index] if item.result for issue in item.result.issues]
            batch_state["batch_parent_diff_files"] = [item.model_dump(mode="json") for item in files]
            batch_state["batch_prior_active_evidence_ids"] = sorted({
                item.evidence_id
                for prior_entry in entries[:index]
                if prior_entry.result and prior_entry.result.active_evidence_set
                for item in prior_entry.result.active_evidence_set.hunks
                if item.evidence_id
            })
            child_snapshot = {"budget": local, "input_fingerprint": unit_execution_fingerprint(
                child.fingerprint, batch_state, self.provider, self.input_mode),
                "model_usages": batch_state.get("model_usages") or []}
            cached = saved.get(entry.id)
            if (cached and cached.result and cached.result.input_fingerprint == child_snapshot["input_fingerprint"]
                    and cached.result.diff_manifest == manifest
                    and cached.result.active_evidence_set == active_evidence_set(manifest, selected, child.id, entry.ranges,
                        [snippet.model_dump(mode="json") for snippet in cached.result.context_snippets])):
                batch_state["review_unit_results"] = [cached.result.model_copy(update={"execution_budget": local}).model_dump(mode="json")]
            token = unit_budget_snapshot.set(child_snapshot)
            try:
                entry.result = await self._execute_unit(child, batch_state)
                batch_execution_complete = (
                    is_review_unit_execution_complete(entry.result)
                    and entry.result.review_summary.status == "reported"
                    and entry.result.review_summary.record is not None
                    and all(
                        check.status == "checked"
                        for check in entry.result.review_summary.record.target_checks
                    )
                    and (
                        entry.result.input_coverage is None
                        or entry.result.input_coverage.target_coverage == "complete"
                    )
                )
                entry.status = "completed" if batch_execution_complete else "partial"
                entry.reason = entry.result.error or entry.result.review_summary.reason
                if entry.result.error and entry.result.error.startswith((
                        "run_budget_admission_rejected", "run_budget_cost_rejected")):
                    entry.status = "skipped"
                    defer(index, entry)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                entry.status, entry.reason = "failed", "review_unit_timed_out"
                raise
            except Exception as exc:
                entry.status, entry.reason = "failed", f"{type(exc).__name__}: {exc}"
                entry.result = ReviewUnitResult(review_unit_id=child.id,
                    input_fingerprint=child_snapshot["input_fingerprint"], status=ReviewUnitStatus.failed,
                    terminal_reason=ReviewUnitTerminalReason.execution_error, error=entry.reason,
                    execution_budget=child_snapshot["budget"],
                    review_summary=self._failure_summary(child_snapshot, entry.reason),
                    evidence_store=child_snapshot.get("evidence_store"), context_selection=child_snapshot.get("context_selection"),
                    model_usages=child_snapshot.get("model_usages") or [])
            finally:
                previous_resources = (budget.model_calls, budget.token_usage, budget.diagnosis_attempts)
                spent = entry.result.execution_budget if entry.result else child_snapshot["budget"]
                budget = budget.model_copy(update={name: getattr(spent, name) for name in (
                    "model_calls", "token_usage", "diagnosis_attempts", "context_retrievals", "patch_attempts")})
                unit_budget_snapshot.reset(token)
                if ((budget.model_calls, budget.token_usage, budget.diagnosis_attempts) != previous_resources
                        or held["model_calls"] or held["token_usage"]):
                    local_revision += 1  # settlement and future holdback release
                if observer is not None:
                    observer.update(budget=budget, diff_batches=entries, evidence_store=merge_stores([
                        observer.get("evidence_store"), child_snapshot.get("evidence_store"), entry.result.evidence_store if entry.result else None]))
        for index in range(len(worksets)):
            await execute_batch(index)
        async def execute_deferred(task):
            await execute_batch(task.payload, draining=True)
            if entries[task.payload].admission.get("deferred_budget"):
                from app.services.review_runtime import ReviewTaskStatus
                task.status = ReviewTaskStatus.DEFERRED_BUDGET

        # Rebuild admission and evidence in execute_batch; never trust a stale
        # admitted=True. Queue revision guards prevent a no-resource busy loop.
        for _ in range(len(entries)):
            before = revision()
            await runtime.deferred.drain(budget_revision=before,
                admit=lambda _task: {"admitted": True}, execute=execute_deferred)
            if revision() == before:
                break
        return self._aggregate_diff_batches(unit, entries, budget, binding, manifest=manifest,
            evidence_store=observer.get("evidence_store") if observer else None)

    @staticmethod
    def _workset_holdback(budget, future, current_tokens, growth):
        """Protect affordable future diagnoses after admitting the current batch first."""
        held = {"model_calls": 0, "diagnosis_attempts": 0, "token_usage": 0, "workset_ids": []}
        for entry in future:
            tokens = entry.admission["reserved_tokens"] + growth
            if budget.can_consume(model_calls=2 + held["model_calls"],
                                  diagnosis_attempts=2 + held["diagnosis_attempts"],
                                  token_usage=current_tokens + tokens + held["token_usage"]):
                held["model_calls"] += 1
                held["diagnosis_attempts"] += 1
                held["token_usage"] += tokens
                held["workset_ids"].append(entry.id)
        return held

    @staticmethod
    def _prior_obligations(entries):
        """先前批次的未闭合契约义务；批次记忆与证据筛选共用同一来源。"""
        obligations = {}
        for entry in entries:
            result = entry.result
            summary = result.review_summary if result is not None else None
            record = (summary.record or summary.last_valid_record) if summary is not None else None
            for dependency in record.contract_dependencies if record is not None else []:
                if dependency.status == "verified":
                    continue
                obligations.setdefault(
                    (dependency.file_path, dependency.symbol, dependency.assumption), dependency)
        return list(obligations.values())

    @staticmethod
    def _batch_memory(entries, ledger):
        from app.models.review import UnitUnresolvedQuestion
        memory = next((item.result.review_summary for item in reversed(entries)
                       if item.result and item.result.review_summary.last_valid_record), None)
        if memory:
            return memory.model_copy(update={"last_valid_record": memory.last_valid_record.model_copy(update={
                "unresolved_questions": [UnitUnresolvedQuestion(id=key, question=item.question,
                    affected_files=item.affected_files, evidence_ids=item.evidence_ids)
                    for key, item in ledger.questions.items() if item.status == "pending"],
                "contract_dependencies": [
                    item.model_copy()
                    for item in ReviewUnitExecutor._prior_obligations(entries)
                ],
            }), "record": None})
        return None

    @staticmethod
    def _batch_supporting_context(
        worksets, entries, index, manifest, current_files, head_sha, base_sha
    ):
        """为后续批次提供同快照的前序 hunk 正文，并绑定原始 evidence ID。

        返回 (support, handoff)：support 是本批次实际携带的正文；handoff 记录本次交接的
        未决义务与“可携带但未携带”的依赖，供聚合阶段判断证据是否确实漏传。
        单条序列化正文超过整段上限时按行分块携带，不整段丢弃；窗口容量由
        _admit_retrieved_context 在到达批次内分批准入。
        """
        support = []
        max_support_chars = 16_000
        max_support_hunks = 4
        current_ranges = {
            (
                item.file_path,
                hunk.new_start,
                hunk.new_start + max(0, hunk.new_length - 1),
            )
            for item in current_files
            for hunk in item.hunks
        }
        obligations = ReviewUnitExecutor._prior_obligations(entries[:index])
        dependency_paths = {dependency.file_path for dependency in obligations}
        prior_indexes = list(range(index - 1, -1, -1))
        if not dependency_paths:
            prior_indexes = prior_indexes[:1]
        candidates = []
        for prior_index in prior_indexes:
            entry = entries[prior_index]
            result = entry.result
            if result is None or result.diff_manifest is None or result.active_evidence_set is None:
                continue
            if (
                result.diff_manifest.input_hash != manifest.input_hash
                or result.diff_manifest.snapshot != manifest.snapshot
                or result.active_evidence_set.manifest_hash != manifest.input_hash
            ):
                continue
            active_ids = {
                item.evidence_id
                for item in result.active_evidence_set.hunks
                if item.evidence_id
            }
            references = {
                item.id: item
                for item in result.review_summary.evidence
                if item.source == "diff" and item.id in active_ids
            }
            for changed_file in worksets[prior_index][1]:
                for hunk in changed_file.hunks:
                    hunk_range = (
                        changed_file.file_path,
                        hunk.new_start,
                        hunk.new_start + max(0, hunk.new_length - 1),
                    )
                    if hunk_range in current_ranges:
                        continue
                    content = json.dumps(
                        hunk.model_dump(), ensure_ascii=False, sort_keys=True
                    )
                    original = make_evidence(
                        changed_file.file_path,
                        "diff",
                        hunk.new_start,
                        hunk.new_start + max(0, hunk.new_length - 1),
                        content,
                        head_sha,
                        base_sha,
                    )
                    if original["id"] not in references:
                        continue
                    if dependency_paths and changed_file.file_path not in dependency_paths:
                        continue
                    snippet = {
                        "file": changed_file.file_path,
                        "start_line": max(1, hunk.new_start),
                        "end_line": max(1, hunk.new_start + max(0, hunk.new_length - 1)),
                        "content": content,
                        "relevance": "contract",
                        "source": "batch_diff_support",
                        "derived_from_evidence_id": original["id"],
                        "review_unit_id": manifest.review_unit_id,
                    }
                    chunked = (ReviewUnitExecutor._chunked_hunk_body(hunk)
                               if len(content) > max_support_chars and hunk.lines else None)
                    candidates.append((snippet, chunked))
        carried_chars = 0
        for snippet, chunked in reversed(candidates):
            if len(support) >= max_support_hunks:
                break
            size = len(snippet["content"])
            if size > max_support_chars:
                if not chunked or not dependency_paths:
                    # 非义务依赖的背景正文维持原上限：可携带但未携带，由 handoff 记录。
                    continue
                # 分块携带保留原证据绑定、快照与行结构；容量准入交给后续批次。
                support.insert(0, {**snippet, "content": chunked,
                                   "end_line": snippet["start_line"] + chunked.count("\n")})
                continue
            if carried_chars + size > max_support_chars:
                continue
            carried_chars += size
            support.insert(0, snippet)
        carried_paths = {item["file"] for item in support}
        carryable_paths = {snippet["file"] for snippet, _ in candidates}
        handoff = {
            "obligations": [contract_dependency_key(item) for item in obligations],
            "carryable_paths": sorted(carryable_paths),
            "dropped_obligations": [
                contract_dependency_key(item)
                for item in obligations
                if item.file_path in carryable_paths and item.file_path not in carried_paths
            ],
        }
        return support, handoff

    @staticmethod
    def _chunked_hunk_body(hunk):
        """按行拆开 hunk 正文；每行沿用 canonical diff 证据的行 JSON 结构。"""
        return "\n".join(
            json.dumps(line.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
            for line in hunk.lines
        )

    @staticmethod
    def _supporting_diff_evidence(state, request):
        """父 Unit 同一 manifest、同一快照内已授权 hunk 的支持性证据。

        只产出绑定原 diff 证据的支持性正文；不改变评论范围、目标覆盖或账本状态。
        """
        from app.models.review import ChangedFile
        from app.services.review_planner import DeterministicReviewPlanner

        parent = state["parent_state"]
        manifest = state.get("diff_manifest")
        current_paths = {file.file_path for file in state["unit_files"]}
        if manifest is None or request.file_path in current_paths:
            return [], []
        source = next((ChangedFile.model_validate(item)
                       for item in parent.get("batch_parent_diff_files") or []
                       if item.get("file_path") == request.file_path), None)
        authorized = set(parent.get("batch_prior_active_evidence_ids") or [])
        if source is None or not authorized:
            return [], []
        by_hunk = {(item.file_path, item.hunk_id): item for item in manifest.hunks}
        requested = set(request.hunk_ids or ())
        snippets, bindings = [], []
        for index, hunk in enumerate(source.hunks):
            identity = DeterministicReviewPlanner.hunk_id(
                request.file_path, index, hunk.model_dump(mode="json"))
            entry = by_hunk.get((request.file_path, identity))
            if (entry is None or entry.evidence_id not in authorized or not hunk.lines
                    or (requested and identity not in requested)):
                continue
            content = json.dumps(hunk.model_dump(), ensure_ascii=False, sort_keys=True)
            reference = make_evidence(request.file_path, "diff", hunk.new_start,
                hunk.new_start + max(0, hunk.new_length - 1), content,
                manifest.snapshot["head_sha"], manifest.snapshot["base_sha"])
            if reference["id"] != entry.evidence_id:
                continue  # 无法重建原绑定时不发出不可核验的证据
            start = max(1, hunk.new_start)
            snippets.append({
                "file": request.file_path,
                "start_line": start,
                "end_line": start + len(hunk.lines) - 1,
                "content": ReviewUnitExecutor._chunked_hunk_body(hunk),
                "relevance": "contract",
                "source": "batch_diff_support",
                "derived_from_evidence_id": reference["id"],
                "review_unit_id": manifest.review_unit_id,
            })
            bindings.append({key: value for key, value in reference.items() if key != "content"})
        return snippets, bindings

    @staticmethod
    def _aggregate_diff_batches(unit, entries, budget, binding, reason=None, manifest=None, evidence_store=None):
        """聚合批次执行、目标覆盖和契约覆盖；三者不再共用一个状态。"""
        from app.review.unit_completion import (
            batch_contract_coverage,
            batch_execution_status,
            batch_target_coverage,
        )

        for entry in entries:
            entry.batch_execution_status = batch_execution_status(entry)
            entry.target_coverage = batch_target_coverage(entry)
            entry.contract_coverage = batch_contract_coverage(entry)
        results = [entry.result for entry in entries if entry.result is not None]
        explicit_failure = reason is not None
        execution_complete = all(
            entry.batch_execution_status == "completed" for entry in entries
        )
        records_complete = all(
            entry.result is not None
            and entry.result.review_summary.status == "reported"
            and entry.result.review_summary.record is not None
            and entry.target_coverage == "complete"
            for entry in entries
        )
        target_complete = bool(entries) and all(
            entry.target_coverage == "complete" for entry in entries
        )
        contract_complete = bool(entries) and all(
            entry.contract_coverage == "complete" for entry in entries
        )
        complete = execution_complete and records_complete and contract_complete and not reason
        ledger = rebuild_coverage(manifest, entries) if manifest else None
        if manifest and unsupported_hunk_ids(manifest):
            reason = reason or NO_HUNK_CHANGE_REASON
            complete = False
        manifest_gaps = (
            classified_coverage_gaps(manifest, ledger)
            if manifest and ledger
            else {"targets": [], "questions": [], "dependencies": []}
        )
        if manifest and manifest_gaps["targets"]:
            complete, reason = False, reason or TARGET_HUNK_UNCOVERED
        records = [result.review_summary.record for result in results if result.review_summary.record]
        evidence = {item.id: item for result in results for item in result.review_summary.evidence}
        summary = UnitReviewSummary(input_protocol=CANONICAL_UNIT_INPUT_PROTOCOL,
            evidence=list(evidence.values()), reason=reason or "unit_diff_batches_incomplete",
            latest_attempt_status="unknown")
        if records_complete and len(records) == len(entries):
            try:
                record = UnitReviewRecord(change_summary="逐批审查记录聚合；结论与输入范围详见批次账本。",
                    target_checks=[check.model_copy(update={"target": f"{entry.id}: {check.target}"})
                        for entry in entries for check in entry.result.review_summary.record.target_checks],
                    hypothesis_checks=[check.model_copy(update={"hypothesis_id": f"{entry.id}: {check.hypothesis_id}"})
                        for entry in entries for check in entry.result.review_summary.record.hypothesis_checks],
                    contract_dependencies=list({(check.file_path, check.symbol, check.assumption): check
                        for record in records for check in record.contract_dependencies}.values()),
                    file_change_checks=list({check.evidence_id: check
                        for record in records for check in record.file_change_checks}.values()),
                    unresolved_questions=list({check.id: check for record in records for check in record.unresolved_questions
                        if not ledger or ledger.questions[check.id].status == "pending"}.values()))
                from app.services.review_input_context import input_snapshot

                previous = results[0].review_summary.latest_attempt_snapshot
                snapshot = input_snapshot({"snapshot": previous,
                    "pr_intent": {"intent_hash": previous.get("pr_intent_hash", "")},
                    "targets": [check.target for check in record.target_checks], "hypotheses": []})
                summary = summary.model_copy(update={"status": "reported",
                    "record": record,
                    "last_valid_record": record, "last_valid_snapshot": snapshot,
                    "latest_attempt_snapshot": snapshot, "latest_attempt_status": "failed" if explicit_failure else "reported",
                    "latest_attempt_reason": reason if explicit_failure else None,
                    "reason": "batch_references_validated_not_correctness_proof" if complete else reason or "unit_diff_batches_incomplete"})
            except ValueError:
                complete, reason = False, "unit_batch_record_aggregate_limit_exceeded"
        evidence_complete = bool(results) and len(results) == len(entries) and all(
            not result.input_coverage
            or result.input_coverage.evidence_coverage == "complete"
            for result in results
        )
        details = classified_coverage_gaps(manifest, ledger) if manifest else {"targets": [], "questions": [], "dependencies": []}
        if not ledger and records:
            details["questions"] = list(dict.fromkeys(question.id or question.question for record in records
                                                     for question in record.unresolved_questions))
            details["dependencies"] = list(dict.fromkeys(check.file_path for record in records
                for check in record.contract_dependencies if check.status != "verified"))
        targets_complete = target_complete and not details["targets"]
        contract_state = contract_coverage_state(
            records,
            [
                evidence.id
                for result in results
                for evidence in result.review_summary.evidence
            ],
            unit,
            manifest,
        )
        contract_evidence_complete = contract_state["evidence_coverage"] == "complete"
        contract_verification_status = contract_state["verification_status"]
        blocking_dependencies = contract_state["blocking_dependencies"]
        contract_gate_complete = not details["questions"] and not blocking_dependencies
        # 只在能确认“更早批次留下了未决依赖、后续批次存在、对应证据可携带却未进入
        # 后续输入”时归因于证据漏传；否则缺口属于契约未决本身。
        dropped_obligations = {
            item
            for entry in entries
            for item in (entry.contract_handoff or {}).get("dropped_obligations", [])
        }
        coverage_reason = (
            EVIDENCE_NOT_CARRIED
            if set(blocking_dependencies) & dropped_obligations
            else CONTRACT_DEPENDENCY_UNRESOLVED
            if blocking_dependencies or details["questions"]
            else BATCH_NO_EVIDENCE_PROGRESS
            if any(entry.batch_execution_status == "no_new_context" for entry in entries)
            else reason or ("unit_diff_batches_complete" if complete else "unit_diff_batches_incomplete")
        )
        coverage = UnitInputCoverage(evidence_coverage="complete" if evidence_complete else "partial",
            target_coverage="complete" if targets_complete else "partial",
            contract_coverage=("partial" if details["questions"] or details["dependencies"] else "complete") if ledger or records else "unknown",
            contract_evidence_coverage=(
                "complete" if contract_evidence_complete else "partial"
            ) if ledger or records else "unknown",
            contract_verification_status=contract_verification_status,
            unresolved_questions=details["questions"],
            unresolved_dependencies=details["dependencies"],
            blocking_dependencies=blocking_dependencies,
            reason=coverage_reason,
            omitted_targets=list(dict.fromkeys([*[
                entry.id for entry in entries if entry.target_coverage != "complete"
            ],
                *details["targets"]])),
            omitted_context=[item for result in results if result.input_coverage
                             for item in result.input_coverage.omitted_context])
        has_progress = any(result.status == ReviewUnitStatus.completed for result in results)
        execution_ready = execution_complete and records_complete and targets_complete and not reason
        if execution_ready and contract_gate_complete and contract_verification_status == "complete":
            completion_status = "complete"
        elif execution_ready and contract_gate_complete:
            completion_status = "complete_with_unresolved_contracts"
        else:
            completion_status = "incomplete"
        return ReviewUnitResult(review_unit_id=unit.id, input_fingerprint=binding,
            status=(ReviewUnitStatus.completed
                    if has_progress else ReviewUnitStatus.timed_out if reason == "review_unit_timed_out" else ReviewUnitStatus.failed),
            unit_completion_status=completion_status,
            terminal_reason=(ReviewUnitTerminalReason.completed
                if has_progress else ReviewUnitTerminalReason.timed_out if reason == "review_unit_timed_out" else ReviewUnitTerminalReason.provider_error),
            error=None if has_progress else reason or "unit_diff_batches_not_executed",
            plan_skipped=True, plan_status=UnitPlanStatus.skipped, plan_skip_reason="bounded_diff_worksets",
            input_coverage=coverage, diff_batches=entries, review_summary=summary, execution_budget=budget,
            diff_manifest=manifest, coverage_ledger=ledger,
            plan=next((item.plan for item in results if item.plan), None),
            issues=[issue.for_review_unit(unit.id) for result in results for issue in result.issues],
            context_snippets=[item for result in results for item in result.context_snippets],
            evidence_store=merge_stores([evidence_store, *[result.evidence_store for result in results]]),
            context_selection=next((result.context_selection for result in reversed(results) if result.context_selection), None),
            messages=[event.model_copy(update={"review_unit_id": unit.id}) for result in results for event in result.messages],
            tool_events=[event.model_copy(update={"review_unit_id": unit.id}) for result in results for event in result.tool_events],
            model_usages=[usage.model_copy(update={"review_unit_id": unit.id}) for result in results for usage in result.model_usages],
            )

    @staticmethod
    def _normalize_graph_state(state: "_ReviewUnitGraphState") -> "_ReviewUnitGraphState":
        """兼容旧 checkpoint：恢复后的 JSON 字典必须回到业务模型。"""
        model_fields = {
            "unit": ReviewUnit,
            "scope": ReviewToolScope,
            "budget": ExecutionBudget,
            "unit_plan": UnitReviewPlan,
            "review_summary": UnitReviewSummary,
            "last_valid_review_summary": UnitReviewSummary,
            "input_coverage": UnitInputCoverage,
            "diff_manifest": UnitDiffManifest,
            "coverage_ledger": UnitCoverageLedger,
            "active_evidence_set": UnitActiveEvidenceSet,
            "evidence_store": UnitEvidenceStore,
            "context_selection": UnitContextSelection,
            "next_action": AgentAction,
        }
        list_fields = {
            "messages": AgentEvent,
            "tool_events": ReviewUnitToolEvent,
            "issues": ReviewIssue,
            "model_usages": ModelUsage,
        }
        normalized = dict(state)
        for key, model in model_fields.items():
            value = normalized.get(key)
            if isinstance(value, dict):
                normalized[key] = model.model_validate(value)
        for key, model in list_fields.items():
            value = normalized.get(key)
            if isinstance(value, list):
                normalized[key] = [
                    item if isinstance(item, model) else model.model_validate(item)
                    for item in value
                ]
        for key, enum_type in (
            ("plan_status", UnitPlanStatus),
            ("terminal_reason", ReviewUnitTerminalReason),
        ):
            value = normalized.get(key)
            if isinstance(value, str):
                normalized[key] = enum_type(value)
        return normalized

    def _build_unit_graph(self) -> StateGraph:
        """构造每个 Review Unit 独立运行的有界 LangGraph 子图。"""
        graph = StateGraph(_ReviewUnitGraphState)
        def normalized_node(handler):
            async def run(state):
                return await handler(self._normalize_graph_state(state))
            return run

        graph.add_node("prepare_unit", normalized_node(self._prepare_unit_node))
        graph.add_node("plan_unit", normalized_node(self._plan_unit_node))
        graph.add_node("agent_decide", normalized_node(self._agent_decide_node))
        graph.add_node("execute_read_tool", normalized_node(self._execute_read_tool_node))
        graph.add_node("report_issue", normalized_node(self._report_issue_node))
        graph.add_node("collect_issue", normalized_node(self._collect_issue_node))
        graph.add_node("finish_unit", normalized_node(self._finish_unit_node))
        graph.add_edge(START, "prepare_unit")
        graph.add_conditional_edges(
            "prepare_unit",
            lambda state: ("report_issue" if self._supports_native_review_step()
                           else "agent_decide" if state["skip_plan"] else "plan_unit"),
            {"plan_unit": "plan_unit", "agent_decide": "agent_decide", "report_issue": "report_issue"},
        )
        graph.add_edge("plan_unit", "agent_decide")
        graph.add_conditional_edges(
            "agent_decide",
            self._route_unit_action,
            UNIT_ACTION_ROUTES,
        )
        graph.add_edge("execute_read_tool", "agent_decide")
        graph.add_edge("report_issue", "collect_issue")
        graph.add_edge("collect_issue", "agent_decide")
        graph.add_conditional_edges("finish_unit", lambda state: END if state.get("done") else "agent_decide",
                                    {END: END, "agent_decide": "agent_decide"})
        return graph

    async def _prepare_unit_node(
        self, state: "_ReviewUnitGraphState"
    ) -> "_ReviewUnitGraphState":
        return {"messages": [*state["messages"], AgentEvent(
            action="prepare_unit",
            reason="建立不可扩张的 Unit 文件范围与执行预算",
            status="completed",
            review_unit_id=state["unit"].id,
        )]}

    async def _plan_unit_node(
        self, state: "_ReviewUnitGraphState"
    ) -> "_ReviewUnitGraphState":
        from app.agents.providers import LLMProviderError
        from app.services.model_usage import annotate_usage, append_usage, unpack_model_call

        budget = state["budget"]
        if not budget.can_consume(model_calls=1):
            return {
                "plan_status": UnitPlanStatus.skipped,
                "plan_skip_reason": "budget_insufficient",
            }
        planning_state = self._unit_state(
            state["parent_state"], state["unit"], state["scope"],
            state["unit_files"], budget, state["context"],
            unit_diff=state["unit_diff"],
            evidence_store=state.get("evidence_store"),
        )
        usage = None
        try:
            raw_result, budget = await self._call_unit_model(
                budget, planning_state, 2_400,
                lambda: self.provider.plan_review_unit(
                    planning_state, state["parent_state"].get("model")),
                holdback=self._diagnosis_holdback(state),
            )
            plan, usage = unpack_model_call(raw_result)
            self._validate_unit_plan_scope(
                plan, state["scope"], planning_state["symbol_index"]
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            budget = getattr(exc, "execution_budget", budget)
            if isinstance(exc, LLMProviderError):
                usage = exc.usage
            usage = annotate_usage(
                usage,
                accounted_tokens_estimate=usage.accounted_tokens_estimate if usage else None,
                review_unit_id=state["unit"].id,
                unit_complexity=state["unit"].complexity,
            )
            detail = f"{type(exc).__name__}: {exc}"
            return {
                "budget": budget,
                "plan_status": (UnitPlanStatus.skipped if getattr(exc, "unit_budget_rejection", None)
                                or "unit_request_budget_exhausted" in str(exc)
                                else UnitPlanStatus.failed),
                "plan_skip_reason": ("diagnosis_budget_protected" if getattr(exc, "unit_budget_rejection", None)
                                     else "budget_insufficient" if "unit_request_budget_exhausted" in str(exc)
                                     else "planning_failed"),
                "plan_error": detail,
                "model_usages": append_usage(state.get("model_usages") or [], usage),
                "messages": [*state["messages"], AgentEvent(
                    action="plan_unit",
                    reason="Unit Plan 生成或校验失败，已降级为无 Plan 审查",
                    status="failed",
                    message=detail,
                    review_unit_id=state["unit"].id,
                )],
            }
        usage = annotate_usage(
            usage,
            accounted_tokens_estimate=usage.accounted_tokens_estimate if usage else None,
            review_unit_id=state["unit"].id,
            unit_complexity=state["unit"].complexity,
        )
        return {
            "unit_plan": plan,
            "plan_status": UnitPlanStatus.planned,
            "plan_skip_reason": None,
            "plan_error": None,
            "next_action": plan.initial_action,
            "budget": budget,
            "model_usages": append_usage(state.get("model_usages") or [], usage),
            "messages": [*state["messages"], AgentEvent(
                action="plan_unit",
                reason=plan.change_summary,
                status="completed",
                message=f"生成 {len(plan.risk_hypotheses)} 个待验证风险假设",
                review_unit_id=state["unit"].id,
            )],
        }

    async def _agent_decide_node(
        self, state: "_ReviewUnitGraphState"
    ) -> "_ReviewUnitGraphState":
        pending = state.get("next_action")
        from app.review.review_policy import REVIEW_STEP_MAX_SAME_EVIDENCE_CALLS
        if self._supports_native_review_step() and not state.get("issue_round_completed") and (
            pending is None or pending.action != AgentActionName.task_done
        ):
            count = state.get("native_review_counts", {}).get(self._native_evidence_key(state), 0)
            if (state.get("retrieval_no_new_rounds", 0) >= 2 and state.get("native_review_counts")) or (
                count >= REVIEW_STEP_MAX_SAME_EVIDENCE_CALLS and (pending is None or pending.action == AgentActionName.report_issue)
            ):
                if (pending is None or pending.action == AgentActionName.report_issue) and self._current_checks_complete(state):
                    return {"next_action": AgentAction(action="task_done", reason="当前检查与契约已完成，结束重复报告"),
                        "issue_round_completed": True,
                        "terminal_reason": ReviewUnitTerminalReason.completed}
                reason = "review_step_no_evidence_progress"
                summary = state.get("review_summary") or UnitReviewSummary()
                if summary.latest_attempt_status not in {"invalid", "failed", "missing"}:
                    summary = summary.model_copy(update={"latest_attempt_status": "unknown",
                        "latest_attempt_reason": reason, "reason": reason})
                self._remember_unit_state({"review_summary": summary})
                return {"next_action": AgentAction(action="task_done", reason="取证未进展，停止重复推理并保留未决"),
                    "review_summary": summary, "latest_review_attempt": {"status": "unknown", "reason": reason},
                    "terminal_reason": ReviewUnitTerminalReason.no_new_context,
                    "messages": [*state.get("messages", []), AgentEvent(action="task_done", status="failed",
                        review_unit_id=state["unit"].id, reason=reason)]}
        if pending is not None:
            return {"next_action": pending}
        if self._supports_native_review_step() and state.get("issue_round_completed"):
            return {"next_action": AgentAction(action=AgentActionName.task_done,
                reason="review_step 已完成当前工作集检查，服务端直接结束本轮")}
        if state.get("batch_decision_disabled") and state.get("issue_round_completed"):
            return {"next_action": AgentAction(action=AgentActionName.task_done,
                reason="当前批次已诊断，可选决策未通过模型窗口准入")}
        if state["issue_round_completed"] and state.get("legacy_review_action", False):
            return {"next_action": AgentAction(
                action=AgentActionName.task_done,
                reason="兼容旧 Provider：完成一次结构化问题报告后显式结束 Unit",
            )}
        if state.get("retrieval_no_new_rounds", 0) >= 2:
            action = AgentAction(
                action=(
                    AgentActionName.task_done
                    if state["issue_round_completed"]
                    else AgentActionName.report_issue
                ),
                reason="连续只读检索未产生新上下文，按服务端策略收敛 Unit",
            )
            return {
                "next_action": action,
                "terminal_reason": (
                    state.get("terminal_reason")
                    or ReviewUnitTerminalReason.no_new_context
                ),
                "messages": [*state["messages"], self._event(
                    state["unit"].id, action, "selected", action.reason
                )],
            }
        if self._supports_native_review_step():
            return {"next_action": AgentAction(action=AgentActionName.report_issue,
                reason="当前工作集证据已更新，继续 review_step 检查")}
        from app.agents.providers import LLMProviderError
        from app.services.model_usage import annotate_usage, append_usage

        try:
            action, budget, legacy, model_usages, terminal_reason = await self._decide_unit(
                state
            )
        except LLMProviderError as exc:
            budget = getattr(exc, "execution_budget", state["budget"])
            usage = annotate_usage(
                exc.usage,
                accounted_tokens_estimate=exc.usage.accounted_tokens_estimate if exc.usage else None,
                review_unit_id=state["unit"].id,
                unit_complexity=state["unit"].complexity,
            )
            detail = f"{type(exc).__name__}: {exc}"
            action = AgentAction(
                action=AgentActionName.task_done,
                reason="模型决策请求失败，终止当前 Review Unit",
            )
            if getattr(exc, "unit_budget_rejection", None) is not None:
                completed = bool(state.get("issue_round_completed"))
                action = AgentAction(action=AgentActionName.task_done if completed else AgentActionName.report_issue,
                    reason="决策额度不足；保留已完成的诊断" if completed else "保护最终诊断预算，跳过可选决策并直接诊断")
                return {"next_action": action, "budget": budget,
                    "model_usages": append_usage(state.get("model_usages") or [], usage),
                    "messages": [*state["messages"], self._event(state["unit"].id, action, "selected", detail)]}
            return {
                "next_action": action,
                "budget": budget,
                "error": detail,
                "terminal_reason": (ReviewUnitTerminalReason.model_budget_exhausted
                                    if "unit_request_budget_exhausted" in detail
                                    else ReviewUnitTerminalReason.provider_error),
                "model_usages": append_usage(
                    state.get("model_usages") or [], usage
                ),
                "messages": [*state["messages"], self._event(
                    state["unit"].id, action, "failed", detail
                )],
            }
        return {
            "next_action": action,
            "budget": budget,
            "legacy_review_action": state.get("legacy_review_action", False) or legacy,
            "model_usages": model_usages,
            "terminal_reason": terminal_reason or state.get("terminal_reason"),
            "messages": [*state["messages"], self._event(
                state["unit"].id, action, "selected", action.reason
            )],
        }

    async def _decide_unit(
        self, state: "_ReviewUnitGraphState"
    ) -> tuple[
        AgentAction,
        ExecutionBudget,
        bool,
        list[dict[str, Any]],
        ReviewUnitTerminalReason | None,
    ]:
        budget = state["budget"]
        if not budget.can_consume(model_calls=1, diagnosis_attempts=1):
            issue_round_completed = bool(state.get("issue_round_completed"))
            summary = state.get("last_valid_review_summary") or state.get("review_summary")
            if not issue_round_completed and summary and summary.last_valid_record and summary.last_valid_snapshot:
                # 恢复中的未知尝试不抹掉同快照的已完成诊断；未决覆盖仍由完成门保留。
                issue_round_completed = self._diagnosis_holdback(state) is None
            if issue_round_completed:
                terminal_reason = (
                    ReviewUnitTerminalReason.completed
                    if state.get("issues")
                    else ReviewUnitTerminalReason.no_issue
                )
                return (
                    AgentAction(
                        action="task_done",
                        reason="剩余额度不能支持新的诊断，按当前检查记录结束 Unit",
                    ),
                    budget,
                    False,
                    list(state.get("model_usages") or []),
                    terminal_reason,
                )
            return (
                AgentAction(action="report_issue", reason="模型调用额度不足，进入明确的诊断准入路径"),
                budget,
                False,
                list(state.get("model_usages") or []),
                None,
            )
        decision_state = self._unit_state(
            state["parent_state"], state["unit"], state["scope"],
            state["unit_files"], budget, state["context"],
            unit_diff=state["unit_diff"],
            unit_plan=state.get("unit_plan"),
            review_summary=state.get("last_valid_review_summary") or state.get("review_summary"),
            latest_attempt=state.get("latest_review_attempt"),
            coverage_ledger=state.get("coverage_ledger"),
            evidence_store=state.get("evidence_store"),
        )
        decision_state.update({
            "unit_agent": True,
            "reported_issue_count": len(state["issues"]),
            "issue_round_completed": state["issue_round_completed"],
            "retrieval_history": list(state["retrieval_history"]),
            "retrieval_no_new_rounds": state["retrieval_no_new_rounds"],
        })
        from app.services.model_usage import annotate_usage, append_usage, unpack_model_call

        raw_result, budget = await self._call_unit_model(
            budget, decision_state, 1_200,
            lambda: self.provider.decide(decision_state, state["parent_state"].get("model")),
            holdback=self._diagnosis_holdback(state),
        )
        action, usage = unpack_model_call(raw_result)
        usage = annotate_usage(
            usage,
            accounted_tokens_estimate=usage.accounted_tokens_estimate if usage else None,
            review_unit_id=state["unit"].id,
            unit_complexity=state["unit"].complexity,
        )
        model_usages = append_usage(state.get("model_usages") or [], usage)
        legacy_review = action.action == AgentActionName.review_code
        if legacy_review:
            action = AgentAction(
                action=(
                    AgentActionName.task_done
                    if state["issue_round_completed"]
                    else AgentActionName.report_issue
                ),
                reason=action.reason,
            )
        elif action.action == AgentActionName.finish_report:
            action = AgentAction(action=AgentActionName.task_done, reason=action.reason)
        if action.action not in UNIT_ALLOWED_ACTIONS:
            action = AgentAction(action=AgentActionName.task_done if state["issue_round_completed"] else AgentActionName.report_issue,
                reason="已拒绝 Unit 白名单外动作；继续只读诊断并在报告中保留不确定性")
        if action.action == AgentActionName.task_done and state["context"] and not state["issue_round_completed"]:
            action = AgentAction(action=AgentActionName.report_issue, reason="新增证据尚未诊断，先完成当前工作集检查")
        return action, budget, legacy_review, model_usages, None

    async def _call_unit_model(self, budget, payload, output_tokens, invoke, *, holdback=None):
        from app.agents.providers import LLMProviderError
        from app.services.model_request_budgeter import UnitRequestLedger
        from app.services.model_usage import unpack_model_call
        from app.services.review_run_budget import current_run_budget, run_budget_attempt_observer

        ledger = UnitRequestLedger(budget, holdback=holdback)
        run = current_run_budget()
        observed = []
        outer_observer = run_budget_attempt_observer.get()
        async def observe(record, admission):
            observed.append(record.attempt_id)
            if outer_observer is not None:
                await outer_observer(record, admission)
        observer_token = run_budget_attempt_observer.set(observe) if run is not None else None

        def project():
            if run is not None and observed:
                records = [run.attempts[key] for key in observed if run.attempts[key].sent]
                ledger.budget = budget.model_copy(update={
                    "model_calls": budget.model_calls + len(records),
                    "token_usage": budget.token_usage + sum(item.actual_tokens if item.actual_tokens is not None
                        else item.reserved_tokens for item in records)})
                ledger.reserved_tokens = sum(item.reserved_tokens for item in records)
        legacy_token = legacy_unit_input_allowed.set(self.input_mode == "legacy")
        try:
            self._check_provider_protocol()
            with ledger.activate():
                if not getattr(self.provider, "supports_request_admission", False):
                    # 旧 Provider 不暴露最终请求；显式近似估算参数，不能声称窗口已验证。
                    chars = len(json.dumps(payload, ensure_ascii=False, default=str))
                    await ledger.reserve({"reserved_tokens": (chars + 3) // 4 + output_tokens})
                result = await invoke()
            _, usage = unpack_model_call(result)
            if ledger.reserved_tokens and run is None:
                ledger.settle(usage)
            project()
            if usage is not None and ledger.reserved_tokens:
                usage.accounted_tokens_estimate = ledger.reserved_tokens
            return result, ledger.budget
        except Exception as exc:
            usage = getattr(exc, "usage", None)
            if ledger.reserved_tokens and run is None:
                ledger.settle(usage)
            project()
            if usage is not None:
                usage.accounted_tokens_estimate = ledger.reserved_tokens
            error = exc if isinstance(exc, LLMProviderError) else LLMProviderError(str(exc))
            error.execution_budget = ledger.budget
            capacity_error = str(exc).startswith(("model_context_window_exceeded", "model_input_limit_exceeded",
                                                 "model_output_limit_exceeded", "required_input_too_large"))
            error.unit_budget_rejection = ledger.rejection or ({"reason": str(exc), "holdback": holdback}
                if not ledger.reserved_tokens and capacity_error else None)
            raise error from exc if error is not exc else None
        finally:
            if observer_token is not None:
                run_budget_attempt_observer.reset(observer_token)
            project()
            legacy_unit_input_allowed.reset(legacy_token)
            from app.services.model_request_budgeter import unit_budget_snapshot

            snapshot = unit_budget_snapshot.get()
            if snapshot is not None:
                snapshot["budget"] = ledger.budget

    def _check_provider_protocol(self):
        from app.agents.providers import LLMProviderError

        method = getattr(self.provider, "review_step", None) if getattr(self.provider, "supports_review_step", False) else getattr(self.provider, "review_unit", None)
        inherited_fallback = getattr(method, "__func__", method) is LLMProvider.review_unit
        if self.input_mode == "canonical" and (getattr(self.provider, "unit_input_protocol", None) != CANONICAL_UNIT_INPUT_PROTOCOL
                                               or not callable(method) or inherited_fallback):
            raise LLMProviderError("canonical_unit_provider_required: Provider must declare canonical-evidence-v3")

    @staticmethod
    def _route_unit_action(state: "_ReviewUnitGraphState") -> str:
        action = state["next_action"].action
        return action.value

    async def _execute_read_tool_node(
        self, state: "_ReviewUnitGraphState"
    ) -> "_ReviewUnitGraphState":
        action = state["next_action"]
        budget = state["budget"]
        events = list(state["tool_events"])
        history = list(state["retrieval_history"])
        if action.action != AgentActionName.retrieve_context:
            return await self._execute_direct_read_action(state, action, budget, events, history)
        if not budget.can_consume(context_retrievals=1):
            events.append(ReviewUnitToolEvent(
                review_unit_id=state["unit"].id,
                tool="code_search",
                status="rejected",
                detail="context retrieval budget exhausted",
            ))
            return {
                "next_action": None,
                "terminal_reason": ReviewUnitTerminalReason.retrieval_budget_exhausted,
                "retrieval_no_new_rounds": state["retrieval_no_new_rounds"] + 1,
                "tool_events": events,
            }
        plan = ContextRetrievalPlan.model_validate(action.tool_args["plan"])
        fingerprint = plan.model_dump_json()
        cached = self._cached_context(state, fingerprint)
        if any(item.get("plan") == fingerprint for item in history) and not cached:
            events.append(ReviewUnitToolEvent(
                review_unit_id=state["unit"].id,
                tool="code_search",
                status="rejected",
                detail="duplicate retrieval plan",
            ))
            history.append({
                "plan": fingerprint,
                "result_count": 0,
                "new_snippet_count": 0,
                "status": "rejected",
            })
            return {
                "next_action": None,
                "retrieval_history": history,
                "retrieval_no_new_rounds": state["retrieval_no_new_rounds"] + 1,
                "tool_events": events,
            }
        return await self._execute_code_search_action(
            state, plan, budget, events, history, fingerprint, cached=cached
        )

    async def _execute_direct_read_action(
        self,
        state: "_ReviewUnitGraphState",
        action: AgentAction,
        budget: ExecutionBudget,
        events: list[ReviewUnitToolEvent],
        history: list[dict[str, Any]],
    ) -> "_ReviewUnitGraphState":
        self._remember_unit_state(state)
        tool_name = action.action.value
        from app.review.run_audit import emit_run_audit

        def observe(status, results=None, reason=None):
            emit_run_audit("tool", review_unit_id=state["unit"].id, tool=tool_name,
                           request=action.tool_args, status=status, results=tool_result_metadata(results or []),
                           reason=reason, scope=state["scope"].model_dump(mode="json"))

        if not budget.can_consume(context_retrievals=1):
            observe("rejected", reason="context retrieval budget exhausted")
            events.append(ReviewUnitToolEvent(
                review_unit_id=state["unit"].id,
                tool=tool_name,
                status="rejected",
                detail="context retrieval budget exhausted",
            ))
            return {
                "next_action": None,
                "terminal_reason": ReviewUnitTerminalReason.retrieval_budget_exhausted,
                "retrieval_no_new_rounds": state["retrieval_no_new_rounds"] + 1,
                "tool_events": events,
            }

        request_payload = action.tool_args["request"]
        fingerprint = json.dumps(
            {"action": tool_name, "request": request_payload},
            ensure_ascii=False,
            sort_keys=True,
        )
        restart_find = (action.action == AgentActionName.file_find and not request_payload.get("cursor")
            and history and history[-1].get("cursor_error")
            and history[-1].get("query") == request_payload.get("query"))
        cached = self._cached_context(state, fingerprint, request_payload if action.action == AgentActionName.file_read else None)
        store = state.get("evidence_store")
        incomplete_read = bool(action.action == AgentActionName.file_read and store and any(
            fingerprint in item.request_fingerprints and item.snippet.truncated
            for item in store.entries.values()))
        if any(item.get("plan") == fingerprint for item in history) and not restart_find and not cached and not incomplete_read:
            observe("rejected", reason="duplicate read request")
            events.append(ReviewUnitToolEvent(
                review_unit_id=state["unit"].id,
                tool=tool_name,
                status="rejected",
                detail="duplicate read request",
            ))
            return {
                "next_action": None,
                "retrieval_history": [*history, {
                    "plan": fingerprint,
                    "result_count": 0,
                    "new_snippet_count": 0,
                    "status": "rejected",
                }],
                "retrieval_no_new_rounds": state["retrieval_no_new_rounds"] + 1,
                "tool_events": events,
            }

        budget = budget.consume(context_retrievals=1)
        context_tool = ScopedContextTool()
        snippets: list[dict[str, Any]] = []
        matches: list[str] = []
        page: dict[str, Any] | None = None
        try:
            if action.action == AgentActionName.file_read:
                request = FileReadRequest.model_validate(request_payload)
                snippets = cached or [await context_tool.file_read(
                    scope=state["scope"],
                    file_path=request.file_path,
                    start_line=request.start_line,
                    end_line=request.end_line,
                )]
            elif action.action == AgentActionName.file_find:
                request = FileFindRequest.model_validate(request_payload)
                page = await context_tool.file_find_page(scope=state["scope"], query=request.query,
                    max_results=request.max_results, cursor=request.cursor)
                matches = page["files"]
            elif action.action == AgentActionName.code_search:
                request = CodeSearchRequest.model_validate(request_payload)
                relation = request.relation
                plan = ContextRetrievalPlan(
                    reason="repository-wide bounded code search",
                    target_symbols=[] if relation == "text" else [request.query],
                    search_terms=[request.query] if relation == "text" else [],
                    relevance_types=[relation],
                    include_callers=relation == "caller",
                    include_callees=relation == "callee",
                    include_tests=relation == "test",
                    max_results=min(request.max_results, state["scope"].max_search_results),
                )
                snippets = cached or await CodeSearchTool().retrieve_context(
                    changed_files=[
                        item.model_dump(mode="json") for item in state["unit_files"]
                    ],
                    symbol_index=state["parent_state"].get("symbol_index") or [],
                    file_index=state["parent_state"].get("file_index") or [],
                    repo_path=state["parent_state"].get("repo_path", ""),
                    plan=plan,
                    scope=state["scope"],
                    repository_graph=state["parent_state"].get("repository_graph") or {},
                )
            elif action.action == AgentActionName.file_read_diff:
                request = FileReadDiffRequest.model_validate(request_payload)
                parent_state = state["parent_state"]
                snippets = [await context_tool.file_read_diff(
                    scope=state["scope"],
                    file_path=request.file_path,
                    changed_files=state["unit_files"],
                    hunk_ids=request.hunk_ids,
                    supporting_files=parent_state.get("batch_parent_diff_files") or [],
                )]
                support_evidence, bindings = self._supporting_diff_evidence(state, request)
                if support_evidence:
                    snippets.extend(support_evidence)
                    registered = {item["id"] for item in parent_state.get("batch_prior_evidence_bindings") or []}
                    parent_state["batch_prior_evidence_bindings"] = [
                        *(parent_state.get("batch_prior_evidence_bindings") or []),
                        *[item for item in bindings if item["id"] not in registered],
                    ]
            else:
                raise ValueError(f"unsupported Unit read action: {tool_name}")

            new_items, admission = self._admit_retrieved_context(state, snippets, budget, request_key=fingerprint)
            admission["cache_hit"] = bool(cached)
            selection_update = self._context_selection_update(state, admission)
            self._remember_unit_state({"budget": budget})
            admission = dict(admission)
            result_count = len(matches) if action.action == AgentActionName.file_find else len(new_items)
            progress_count = result_count
            if action.action == AgentActionName.file_find and self._supports_native_review_step():
                progress_count = len(set(matches) - self._file_find_paths(events))
            observe("completed", results=matches if tool_name == "file_find" else snippets)
            events.append(ReviewUnitToolEvent(
                review_unit_id=state["unit"].id,
                tool=tool_name,
                status=admission["status"],
                result_count=result_count,
                detail=json.dumps(page if page is not None else admission, ensure_ascii=False),
            ))
            history_item: dict[str, Any] = {
                "plan": fingerprint,
                "result_count": result_count,
                "new_snippet_count": len(new_items),
                "status": admission["status"],
                "context_admission": admission,
            }
            if matches:
                history_item["matches"] = matches
            if page is not None:
                history_item["page"] = {key: value for key, value in page.items() if key != "files"}
            return {
                **selection_update,
                "next_action": self._retrieval_next_action(state, budget, admission),
                "budget": budget,
                "input_coverage": self._retrieval_input_coverage(state, admission),
                "issue_round_completed": False if admission["active_set_changed"] else state.get("issue_round_completed", False),
                "retrieval_history": [*history, history_item],
                "retrieval_no_new_rounds": (
                    0 if progress_count else state["retrieval_no_new_rounds"] + 1
                ),
                "tool_events": events,
            }
        except ValueError as exc:
            observe("rejected", reason=str(exc))
            events.append(ReviewUnitToolEvent(
                review_unit_id=state["unit"].id,
                tool=tool_name,
                status="rejected",
                detail=str(exc),
            ))
            return {
                "next_action": None,
                "budget": budget,
                "retrieval_history": [*history, {
                    "plan": fingerprint,
                    "result_count": 0,
                    "new_snippet_count": 0,
                    "status": "rejected",
                    **({"cursor_error": True, "query": request_payload.get("query")}
                       if str(exc).startswith("file_find_cursor_invalid_or_expired") else {}),
                }],
                "retrieval_no_new_rounds": state["retrieval_no_new_rounds"] + 1,
                "tool_events": events,
            }

    async def _execute_code_search_action(
        self,
        state: "_ReviewUnitGraphState",
        plan: ContextRetrievalPlan,
        budget: ExecutionBudget,
        events: list[ReviewUnitToolEvent],
        history: list[dict[str, Any]],
        fingerprint: str,
        *, cached=None,
    ) -> "_ReviewUnitGraphState":
        budget = budget.consume(context_retrievals=1)
        from app.review.run_audit import emit_run_audit

        try:
            snippets = cached or await CodeSearchTool().retrieve_context(
                changed_files=[item.model_dump(mode="json") for item in state["unit_files"]],
                symbol_index=state["parent_state"].get("symbol_index") or [],
                file_index=state["parent_state"].get("file_index") or [],
                repo_path=state["parent_state"].get("repo_path", ""),
                plan=plan,
                scope=state["scope"],
                repository_graph=state["parent_state"].get("repository_graph") or {},
            )
            new_items, admission = self._admit_retrieved_context(state, snippets, budget, request_key=fingerprint)
            admission["cache_hit"] = bool(cached)
            selection_update = self._context_selection_update(state, admission)
            self._remember_unit_state({"budget": budget})
            admission = dict(admission)
            emit_run_audit("tool", review_unit_id=state["unit"].id, tool="code_search",
                           request=plan.model_dump(mode="json"), status="completed", results=tool_result_metadata(snippets),
                           scope=state["scope"].model_dump(mode="json"))
            events.append(ReviewUnitToolEvent(
                review_unit_id=state["unit"].id,
                tool="code_search",
                status=admission["status"],
                result_count=len(new_items),
                detail=json.dumps(admission, ensure_ascii=False),
            ))
            history.append({
                "plan": fingerprint,
                "result_count": len(snippets),
                "new_snippet_count": len(new_items),
                "truncated_count": sum(
                    1 for item in snippets
                    if item.get("content", "").endswith("...(truncated)")
                ),
                "status": admission["status"],
                "context_admission": admission,
            })
            return {
                **selection_update,
                "next_action": self._retrieval_next_action(state, budget, admission),
                "budget": budget,
                "input_coverage": self._retrieval_input_coverage(state, admission),
                "issue_round_completed": False if admission["active_set_changed"] else state.get("issue_round_completed", False),
                "retrieval_history": history,
                "retrieval_no_new_rounds": (
                    0 if new_items else state["retrieval_no_new_rounds"] + 1
                ),
                "tool_events": events,
            }
        except ValueError as exc:
            emit_run_audit("tool", review_unit_id=state["unit"].id, tool="code_search",
                           request=plan.model_dump(mode="json"), status="rejected", reason=str(exc),
                           results=[], scope=state["scope"].model_dump(mode="json"))
            events.append(ReviewUnitToolEvent(
                review_unit_id=state["unit"].id,
                tool="code_search",
                status="rejected",
                detail=str(exc),
            ))
            history.append({
                "plan": fingerprint,
                "result_count": 0,
                "new_snippet_count": 0,
                "status": "rejected",
            })
            return {
                "next_action": None,
                "budget": budget,
                "retrieval_history": history,
                "retrieval_no_new_rounds": state["retrieval_no_new_rounds"] + 1,
                "tool_events": events,
            }

    async def _report_issue_node(
        self, state: "_ReviewUnitGraphState"
    ) -> "_ReviewUnitGraphState":
        self._remember_unit_state(state)
        budget = state["budget"]
        if (not budget.can_consume(diagnosis_attempts=1, model_calls=1)
                or (self._supports_native_review_step() and state["parent_state"].get("cross_unit_followup") and budget.diagnosis_attempts >= 1)):
            return {
                "pending_issues": [],
                "next_action": None,
                "terminal_reason": ReviewUnitTerminalReason.diagnosis_budget_exhausted,
                "error": "unit_diagnosis_budget_exhausted: no diagnosis attempt or model call available",
            }
        budget = budget.consume(diagnosis_attempts=1)
        self._remember_unit_state({"budget": budget})
        from app.services.model_usage import annotate_usage, append_usage, unpack_model_call
        from app.review.issue_audit import issue_audit_unit

        from app.agents.providers import LLMProviderError
        from app.services.model_request_budgeter import unit_budget_snapshot

        observer = unit_budget_snapshot.get()
        if observer is not None:
            observer["diagnosis_pending"] = True
        args = self._diagnosis_args(state)
        record_input = args[-1]
        input_coverage = state.get("input_coverage")
        try:
            args, degradation = self._select_diagnosis_input(state, args=args)
            if degradation:
                memory = record_input.get("working_memory") or {}
                omitted_targets = list(dict.fromkeys([
                    *(target for target in record_input["targets"] if target not in args[-1]["targets"]),
                    *(item["description"] for item in record_input["hypotheses"]),
                    *(item["target"] for item in memory.get("target_checks", [])),
                    *(item["question"] for item in memory.get("unresolved_questions", [])),
                    *(input_coverage.omitted_targets if input_coverage else []),
                ]))
                input_coverage = UnitInputCoverage(
                    evidence_coverage="partial" if input_coverage and input_coverage.omitted_context else "complete",
                    reason=(input_coverage.reason if input_coverage and input_coverage.omitted_context else DIAGNOSIS_BACKGROUND_DEGRADED),
                    omitted_components=degradation["omitted"], omitted_targets=omitted_targets,
                    omitted_plan=state.get("unit_plan") or (input_coverage.omitted_plan if input_coverage else None),
                    omitted_context=input_coverage.omitted_context if input_coverage else [],
                )
                self._remember_unit_state({"input_coverage": input_coverage})
            record_input = args[-1]
            with issue_audit_unit(state["unit"].id):
                async def invoke():
                    if self._supports_native_review_step():
                        return await self.provider.review_step(*args)
                    return await self.provider.review_unit(*args)
                from app.review.review_policy import REVIEW_STEP_OUTPUT_TOKENS
                repair_usages, repair_attempted, diagnosis_calls = [], False, 1
                if self._supports_native_review_step():
                    raw_result, budget, repair_usages, repair_attempted, diagnosis_calls = await self._review_with_output_repair(
                        state, args, budget, REVIEW_STEP_OUTPUT_TOKENS)
                else:
                    raw_result, budget = await self._call_unit_model(budget, record_input, REVIEW_STEP_OUTPUT_TOKENS, invoke)
        except LLMProviderError as exc:
            usage = annotate_usage(
                exc.usage,
                accounted_tokens_estimate=exc.usage.accounted_tokens_estimate if exc.usage else None,
                review_unit_id=state["unit"].id, unit_complexity=state["unit"].complexity,
            )
            raw_rejection = getattr(exc, "admission_rejection", None)
            rejection = UnitBudgetRejection.model_validate(raw_rejection) if raw_rejection is not None else None
            failed_summary = validate_record(None, record_input, state["scope"].readable_files, str(exc))
            failed_summary = failed_summary.model_copy(update={"latest_attempt_status": "failed"})
            failed_summary = merge_review_summaries(state.get("review_summary") or UnitReviewSummary(), failed_summary, None)
            if observer is not None:
                observer.update(review_summary=failed_summary, diagnosis_pending=False)
            return {
                "review_summary": failed_summary,
                "pending_issues": [], "next_action": None,
                "budget": getattr(exc, "execution_budget", budget), "error": str(exc),
                "budget_rejection": rejection,
                "terminal_reason": (ReviewUnitTerminalReason.diagnosis_budget_exhausted
                                    if "unit_request_budget_exhausted" in str(exc)
                                    else ReviewUnitTerminalReason.provider_error),
                "model_usages": append_usage([*(state.get("model_usages") or []), *getattr(exc, "output_repair_usages", [])], usage),
                "output_repair_attempted": state.get("output_repair_attempted", False) or getattr(exc, "output_repair_attempted", False),
                "latest_review_attempt": {"status": "failed", "reason": str(exc)},
                "input_coverage": input_coverage,
            }
        response, usage = unpack_model_call(raw_result)
        native_update = {}
        if self._supports_native_review_step():
            key = self._native_evidence_key(state)
            counts = dict(state.get("native_review_counts") or {})
            counts[key] = counts.get(key, 0) + diagnosis_calls
            native_update = {"native_review_counts": counts, "last_review_evidence_key": key,
                "last_review_requires_context": bool(response.next_need_error or (response.next_need
                    and response.next_need.action not in {AgentActionName.report_issue, AgentActionName.task_done}))}
        if self.input_mode == "legacy":
            response = response.model_copy(update={"review_record": None,
                "record_error": "legacy_provider_without_canonical_evidence_protocol"})
        from app.review.issue_audit import audit_issue

        if not response.issues_audited:
            for issue in response.issues:
                audit_issue("generated", issue.for_review_unit(state["unit"].id))
        from app.services.unit_review_summary import normalize_review_targets
        response = response.model_copy(update={"review_record": normalize_review_targets(response.review_record, record_input)})
        summary = validate_record(
            response.review_record, record_input, state["scope"].readable_files,
            response.record_error,
        )
        if self.input_mode == "legacy":
            summary = summary.model_copy(update={"latest_attempt_status": "unknown"})
        latest_attempt = {"status": summary.latest_attempt_status or summary.status, "reason": summary.reason}
        summary = merge_review_summaries(
            state.get("review_summary") or state.get("last_valid_review_summary") or UnitReviewSummary(),
            summary, response.review_record,
        )
        if response.next_need_error:
            summary = summary.model_copy(update={"latest_attempt_status": "invalid",
                "latest_attempt_reason": response.next_need_error, "reason": response.next_need_error})
            latest_attempt = {"status": "invalid", "reason": response.next_need_error}
        if input_coverage and input_coverage.target_coverage != "complete" and summary.status == "reported":
            # 核心证据诊断可产生候选，但省略的检查目标/工作记忆不能被宣称为完整覆盖。
            summary = summary.model_copy(update={"status": "unknown", "record": None,
                "latest_attempt_status": "unknown", "latest_attempt_reason": input_coverage.reason,
                "reason": input_coverage.reason})
            latest_attempt = {"status": "unknown", "reason": summary.reason}
            input_coverage = input_coverage.model_copy(update={"target_coverage": "partial"})
        usage = annotate_usage(
            usage,
            accounted_tokens_estimate=usage.accounted_tokens_estimate if usage else None,
            review_unit_id=state["unit"].id,
            unit_complexity=state["unit"].complexity,
        )
        previous_usages = [*(state.get("model_usages") or []), *repair_usages]
        self._remember_unit_state({"review_summary": summary,
            "input_coverage": input_coverage,
            **({"unit_plan": None, "plan_status": UnitPlanStatus.skipped} if degradation else {}),
            "model_usages": append_usage(previous_usages, usage)})
        if observer is not None:
            observer["diagnosis_pending"] = False
        hierarchy_update = {}
        if state.get("diff_manifest"):
            active = active_evidence_set(state["diff_manifest"], state["unit_files"], state["unit"].id,
                (state["parent_state"].get("unit_diff_batch") or {}).get("ranges"), state["context"])
            hierarchy_update = {"active_evidence_set": active, "coverage_ledger": update_coverage(
                state["coverage_ledger"], state["diff_manifest"], active, summary, state.get("unit_plan"))}
            self._remember_unit_state(hierarchy_update)
        return {
            **hierarchy_update,
            **native_update,
            "output_repair_attempted": state.get("output_repair_attempted", False) or repair_attempted,
            "pending_issues": response.issues,
            "input_coverage": input_coverage,
            **({"unit_plan": None, "plan_status": UnitPlanStatus.skipped,
                "plan_skip_reason": "diagnosis_background_budget_degraded",
                "messages": [*state.get("messages", []), AgentEvent(action="report_issue", status="selected",
                    review_unit_id=state["unit"].id, reason="诊断预算降级：保留完整证据，移除可选规划与工作记忆",
                    message=json.dumps(degradation, ensure_ascii=False))]} if degradation else {}),
            "review_summary": summary,
            "last_valid_review_summary": (summary if latest_attempt["status"] == "reported"
                                          else state.get("last_valid_review_summary")),
            "latest_review_attempt": latest_attempt,
            "budget": budget,
            "model_usages": append_usage(previous_usages, usage),
            "next_action": (AgentAction(action="task_done", reason="下一步动作格式无效，拒绝执行并保留当前候选")
                            if response.next_need_error else response.next_need),
            "budget_rejection": None,
        }

    async def _review_with_output_repair(self, state, args, budget, output_tokens):
        from app.agents.providers import LLMProviderError
        from app.models.review import ModelCallResult
        from app.services.model_usage import unpack_model_call, annotate_usage
        from app.services.unit_review_summary import domain_output_errors
        from app.review.run_audit import emit_run_audit

        original, retained, attempted, calls = None, [], False, 0
        current_args = args
        while True:
            calls += 1
            before_calls = budget.model_calls
            try:
                raw, budget = await self._call_unit_model(budget, current_args[-1], output_tokens,
                    lambda: self.provider.review_step(*current_args))
                response, usage = unpack_model_call(raw)
                errors = response.output_schema_errors
                checked = validate_record(response.review_record, args[-1], state["scope"].readable_files, response.record_error)
                if not errors:
                    errors = domain_output_errors(checked.latest_attempt_reason)
                if attempted:
                    preserve = {"issues": original.issues if original else [], "issues_audited": bool(original and original.issues_audited)}
                if attempted and original is not None:
                    original_check = validate_record(original.review_record, args[-1], state["scope"].readable_files, original.record_error)
                    if original_check.status == "reported":
                        preserve.update(review_record=original.review_record, record_error=original.record_error)
                    if not original.next_need_error:
                        preserve.update(next_need=original.next_need, next_need_error=None)
                if attempted:
                    response = response.model_copy(update=preserve)
                if not errors or attempted:
                    if attempted:
                        emit_run_audit("output_repair", review_unit_id=state["unit"].id, status="completed" if not errors else "invalid")
                    return ModelCallResult(response, usage), budget, retained, attempted, calls
                original = response
            except LLMProviderError as exc:
                budget = getattr(exc, "execution_budget", budget)
                if attempted and budget.model_calls == before_calls and getattr(exc, "unit_budget_rejection", None):
                    calls -= 1
                    budget = budget.model_copy(update={"diagnosis_attempts": max(0, budget.diagnosis_attempts - 1)})
                    attempted = False
                    self._remember_unit_state({"budget": budget, "output_repair_attempted": False})
                    emit_run_audit("output_repair", review_unit_id=state["unit"].id, status="budget_rejected")
                    if original:
                        return ModelCallResult(original, exc.usage), budget, retained, attempted, calls
                    exc.output_repair_usages, exc.output_repair_attempted = retained, False
                    raise
                usage = exc.usage
                errors = getattr(exc, "output_schema_errors", [])
                if attempted or not errors:
                    emit_run_audit("output_repair", review_unit_id=state["unit"].id, status="failed", error_type=type(exc).__name__)
                    if original is not None:
                        return ModelCallResult(original.model_copy(update={"next_need_error": original.next_need_error or "output_repair_failed"}), usage), budget, retained, attempted, calls
                    exc.output_repair_usages, exc.output_repair_attempted = retained, attempted
                    raise
            if (state.get("output_repair_attempted") or attempted or usage is None or not usage.usage_available
                    or not budget.can_consume(model_calls=1, diagnosis_attempts=1)
                    or any("requires evidence" in item.get("message", "") for item in errors)):
                if original is not None:
                    return ModelCallResult(original, usage), budget, retained, attempted, calls
                error = LLMProviderError("invalid model output; corrective attempt unavailable", usage=usage)
                error.execution_budget = budget
                raise error
            feedback = {"attempt": 1, "errors": errors[:10]}
            corrective_args = (*args[:-1], {**args[-1], "output_repair": feedback})
            from app.services.review_run_budget import current_run_budget
            try:
                required = self._diagnosis_estimate(corrective_args)["reserved_tokens"]
                run = current_run_budget()
                available = run.available() if run else None
                admitted = budget.can_consume(model_calls=1, diagnosis_attempts=1, token_usage=required) and (
                    available is None or (available["calls"] > 0 and available["tokens"] >= required))
            except LLMProviderError:
                admitted = False
            if not admitted:
                if original:
                    return ModelCallResult(original, usage), budget, retained, attempted, calls
                error = LLMProviderError("invalid model output; corrective request not admitted", usage=usage)
                error.execution_budget = budget
                raise error
            retained.append(annotate_usage(usage, accounted_tokens_estimate=usage.accounted_tokens_estimate,
                review_unit_id=state["unit"].id, unit_complexity=state["unit"].complexity).model_dump(mode="json"))
            budget = budget.consume(diagnosis_attempts=1)
            attempted = True
            if original:
                from app.review.issue_audit import audit_issue
                for issue in original.issues:
                    audit_issue("generated", issue.for_review_unit(state["unit"].id))
                original = original.model_copy(update={"issues_audited": True})
                self._remember_unit_state({"issues": [*state.get("issues", []), *(issue.for_review_unit(state["unit"].id) for issue in original.issues)]})
            self._remember_unit_state({"budget": budget, "output_repair_attempted": True,
                "model_usages": [*(state.get("model_usages") or []), *retained]})
            current_args = corrective_args
            emit_run_audit("output_repair", review_unit_id=state["unit"].id, status="started", diagnostics=feedback)

    @staticmethod
    def _current_checks_complete(state):
        summary = state.get("review_summary")
        if (state.get("last_review_requires_context", True)
                or not summary or summary.status != "reported" or summary.latest_attempt_status != "reported"
                or not summary.record or state.get("last_review_evidence_key") != ReviewUnitExecutor._native_evidence_key(state)
                or summary.last_valid_snapshot.get("head_sha") != state["parent_state"].get("head_sha")
                or summary.last_valid_snapshot.get("base_sha", "") != review_diff_base(state["parent_state"])):
            return False
        record = summary.record
        if (not record.target_checks or any(check.status != "checked" or not check.evidence_ids for check in record.target_checks)
                or record.unresolved_questions or any(check.status != "verified" for check in record.contract_dependencies)
                or any(check.status not in {"supported", "refuted"} for check in record.hypothesis_checks)):
            return False
        coverage = state.get("input_coverage")
        if coverage and (coverage.omitted_targets or coverage.omitted_context or coverage.omitted_components):
            return False
        manifest, ledger = state.get("diff_manifest"), state.get("coverage_ledger")
        if manifest is None or ledger is None:
            return False
        active = state.get("active_evidence_set") if manifest.review_unit_id != state["unit"].id else None
        return not coverage_gaps(manifest, ledger, active, state.get("unit_plan"))

    @staticmethod
    def _file_find_paths(events):
        found_paths = set()
        for event in events:
            if event.tool == "file_find" and event.result_count:
                try:
                    page = json.loads(event.detail or "{}")
                    if isinstance(page, dict):
                        found_paths.update(path for path in page.get("files", []) if isinstance(path, str))
                except (ValueError, TypeError):
                    pass
        return found_paths

    @staticmethod
    def _native_evidence_key(state):
        from app.services.fingerprints import stable_hash
        contexts = [{key: item.get(key) for key in ("file", "start_line", "end_line", "source", "content")}
                    for item in state.get("context", [])]
        contexts.sort(key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
        return stable_hash({"files": [file.model_dump(mode="json") for file in state["unit_files"]],
            "context": contexts, "found_paths": sorted(ReviewUnitExecutor._file_find_paths(state.get("tool_events", []))),
            "head": state["parent_state"].get("head_sha"),
            "base": review_diff_base(state["parent_state"])})

    @staticmethod
    def _carried_obligations(state, record_input) -> list[dict[str, Any]]:
        """跨批次未决契约义务的显式交接通道；与可携带的正文片段解耦。

        义务本身始终保留原始 assumption；只有当前输入仍可引用的 evidence ID 才随义务传递。
        """
        available = {item["id"] for item in record_input["evidence"]}
        sources = [
            state.get("prior_contract_dependencies"),
            state["parent_state"].get("prior_contract_dependencies"),
            getattr(state["parent_state"].get("batch_memory_summary"), "last_valid_record", None),
        ]
        carried: dict[tuple, dict[str, Any]] = {}
        for source in sources:
            if source is None:
                continue
            for item in getattr(source, "contract_dependencies", source) or []:
                payload = item.model_dump(mode="json") if hasattr(item, "model_dump") else item
                if payload.get("status", "unresolved") == "verified":
                    continue
                payload = {**payload,
                    "evidence_ids": [identity for identity in payload.get("evidence_ids") or []
                                     if identity in available]}
                carried.setdefault(
                    (payload.get("file_path"), payload.get("symbol"), payload.get("assumption")), payload)
        return list(carried.values())

    def _diagnosis_args(self, state, *, core=False):
        pr = PullRequestInfo.model_validate(state["parent_state"].get("pr_info") or {})
        plan = None if core else state.get("unit_plan")
        record_input = build_record_input(state["unit_files"], state["context"], plan,
            str(state["parent_state"].get("head_sha") or ""), review_diff_base(state["parent_state"]))
        from app.services.file_change_evidence import set_file_change_impact_scope
        set_file_change_impact_scope(record_input, getattr(state["unit"], "related_files", []))
        record_input["pr_intent"] = build_pr_intent(pr)
        if self._supports_native_review_step():
            from app.services.unit_review_summary import review_target_catalog
            record_input["target_catalog"] = review_target_catalog(record_input["targets"])
        if state["parent_state"].get("origin_obligations"):
            record_input["origin_obligations"] = state["parent_state"]["origin_obligations"]
        if state["parent_state"].get("batch_prior_evidence_bindings"):
            record_input["prior_evidence_bindings"] = state["parent_state"]["batch_prior_evidence_bindings"]
        if state.get("diff_manifest"):
            record_input.update(diff_manifest=state["diff_manifest"].model_dump(mode="json"),
                coverage_ledger=state["coverage_ledger"].model_dump(mode="json"),
                unit_metadata=state["parent_state"]["unit_metadata"],
                active_evidence_set=active_evidence_set(state["diff_manifest"], state["unit_files"], state["unit"].id,
                    (state["parent_state"].get("unit_diff_batch") or {}).get("ranges"), state["context"]).model_dump(mode="json"))
        if state["parent_state"].get("unit_diff_batch"):
            record_input["diff_workset"] = state["parent_state"]["unit_diff_batch"]
        record_input["input_protocol"] = CANONICAL_UNIT_INPUT_PROTOCOL if self.input_mode == "canonical" else "legacy"
        record_input["working_memory"] = {} if core else build_working_memory(
            state.get("last_valid_review_summary") or state["parent_state"].get("batch_memory_summary") or state.get("review_summary"), record_input,
            state["unit"].id, state.get("latest_review_attempt"))[0]
        # 未决义务与可核验正文分开交接：没有绑定正文时，义务也必须显式传给后续批次。
        # 降级输入（core）保持原有取舍，不额外加入义务通道。
        prior_dependencies = [] if core else self._carried_obligations(state, record_input)
        if prior_dependencies:
            record_input["prior_contract_dependencies"] = prior_dependencies
            record_input["working_memory"] = {
                key: value
                for key, value in record_input["working_memory"].items()
                if key != "contract_dependencies"
            }
        feedback = [{"tool": event.tool, "status": event.status, "detail": (event.detail or "")[:240],
                     "result_count": event.result_count} for event in state.get("tool_events", [])[-3:]]
        if feedback and self._supports_native_review_step():
            for item, event in zip(feedback, state.get("tool_events", [])[-3:]):
                if event.tool == "file_find" and event.result_count:
                    try:
                        page = json.loads(event.detail or "{}")
                        if isinstance(page, dict):
                            item["files"] = [path for path in page.get("files", []) if isinstance(path, str)]
                    except (ValueError, TypeError):
                        pass
            record_input["tool_feedback"] = feedback
            record_input["recent_tool_requests"] = [item["plan"][:500] for item in state.get("retrieval_history", [])[-3:]
                                                   if isinstance(item.get("plan"), str)]
        if self._supports_native_review_step():
            record_input["readonly_tool_scope"] = {"file_read_diff": {
                file.file_path: [self.planner.hunk_id(file.file_path, index, hunk.model_dump(mode="json"))
                                 for index, hunk in enumerate(file.hunks)] for file in state["unit_files"]}}
            parent_files = state["parent_state"].get("batch_parent_diff_files")
            if parent_files and state.get("diff_manifest") is not None:
                current_paths = {file.file_path for file in state["unit_files"]}
                authorized = set(state["parent_state"].get("batch_prior_active_evidence_ids") or [])
                supporting_scope: dict[str, list[str]] = {}
                for item in state["diff_manifest"].hunks:
                    if item.evidence_id in authorized and item.file_path not in current_paths:
                        supporting_scope.setdefault(item.file_path, []).append(item.hunk_id)
                if supporting_scope:
                    record_input["supporting_diff_scope"] = supporting_scope
        language = build_language_context((item.file_path for item in state["unit_files"]),
            state["parent_state"].get("file_index") or [], state["parent_state"].get("project_meta") or {})
        record_input["review_guidance"] = render_language_rule_context(language)
        if core:
            record_input["input_degradation"] = {"reason": "diagnosis_background_budget_degraded",
                "omitted": ["unit_plan", "working_memory"], "evidence_coverage": "complete"}
        return (pr, state["unit_files"], self._enhanced_diff(state["unit_diff"], state["context"], plan, language),
                state["parent_state"].get("model"), record_input)

    def _diagnosis_estimate(self, args):
        estimator_name = "unit_review_step_admission" if self._supports_native_review_step() else "unit_diagnosis_admission"
        estimator = getattr(self.provider, estimator_name, None)
        if callable(estimator):
            token = legacy_unit_input_allowed.set(self.input_mode == "legacy")
            try:
                return estimator(*args)
            finally:
                legacy_unit_input_allowed.reset(token)
        chars = len(json.dumps(args[-1], ensure_ascii=False, default=str))
        return {"reserved_tokens": (chars + 3) // 4 + 4096, "count_method": "serialized_unit_arguments"}

    def _select_diagnosis_input(self, state, *, args=None):
        from app.agents.providers import LLMProviderError

        args = args or self._diagnosis_args(state)
        try:
            estimate = self._diagnosis_estimate(args)
            if state["budget"].can_consume(model_calls=1, token_usage=estimate["reserved_tokens"]):
                return args, None
        except LLMProviderError as exc:
            if not str(exc).startswith(("model_context_window_exceeded", "model_input_limit_exceeded",
                                        "model_output_limit_exceeded", "required_input_too_large")):
                raise
        # 仅降级可选背景。完整 Diff、检索正文、证据 ID/哈希和作者背景始终保留。
        if not state["parent_state"].get("cross_unit_followup") and (
            state.get("unit_plan") or args[-1].get("working_memory")
        ):
            core = self._diagnosis_args(state, core=True)
            try:
                estimate = self._diagnosis_estimate(core)
                if state["budget"].can_consume(model_calls=1, token_usage=estimate["reserved_tokens"]):
                    return core, core[-1]["input_degradation"]
            except LLMProviderError as exc:
                if not str(exc).startswith(("model_context_window_exceeded", "model_input_limit_exceeded",
                                            "model_output_limit_exceeded", "required_input_too_large")):
                    raise
        return args, None

    def _diagnosis_holdback(self, state):
        if state.get("issue_round_completed"):
            return None
        # 恢复中的一次后续失败不能抹掉此前已完成的同快照诊断，也不能预留已无必要的额外调用。
        summary = state.get("last_valid_review_summary") or state.get("review_summary")
        if summary and summary.last_valid_record and summary.last_valid_snapshot:
            from app.services.review_input_context import input_snapshot

            current_input = self._diagnosis_args(state)[-1]
            if summary.last_valid_snapshot == input_snapshot(current_input) and (
                {item["id"] for item in current_input["evidence"]} <= {item.id for item in summary.evidence}
            ):
                return None
        from app.agents.providers import LLMProviderError

        try:
            args, _ = self._select_diagnosis_input(state)
            estimate = self._diagnosis_estimate(args)
        except LLMProviderError as exc:
            if str(exc).startswith(("model_context_window_exceeded", "model_input_limit_exceeded",
                                    "model_output_limit_exceeded", "required_input_too_large")):
                exc.unit_budget_rejection = {"reason": "diagnosis_capacity_rejected", "operation": "diagnosis"}
            raise
        return {"operation": "diagnosis", "model_calls": 1,
            "token_usage": estimate["reserved_tokens"], "count_method": estimate["count_method"]}

    @staticmethod
    def _remember_unit_state(state):
        from app.services.model_request_budgeter import unit_budget_snapshot

        observer = unit_budget_snapshot.get()
        if observer is not None:
            observer.update({key: state[key] for key in ("review_summary", "context", "unit_plan", "plan_status", "model_usages", "budget", "input_coverage", "diff_manifest", "coverage_ledger", "active_evidence_set", "evidence_store", "context_selection", "issues", "output_repair_attempted") if key in state})

    @staticmethod
    def _failure_summary(snapshot, reason):
        summary = snapshot.get("review_summary") or UnitReviewSummary()
        if snapshot.get("diagnosis_pending"):
            return summary.model_copy(update={"status": "unknown", "record": None,
                "latest_attempt_status": "failed", "latest_attempt_reason": reason, "reason": reason})
        return summary

    async def _collect_issue_node(
        self, state: "_ReviewUnitGraphState"
    ) -> "_ReviewUnitGraphState":
        if state.get("error") or state.get("terminal_reason") == ReviewUnitTerminalReason.diagnosis_budget_exhausted:
            return {"pending_issues": [], "next_action": AgentAction(
                action=AgentActionName.task_done, reason="诊断未完成，保留失败或预算拒绝状态")}
        accepted = self._filter_issues(
            state.get("pending_issues") or [], state["unit"], state["scope"]
        )
        known = {issue.id for issue in state["issues"]}
        from app.review.issue_audit import audit_issue

        for issue in accepted:
            if issue.id in known:
                audit_issue("unit_filter", issue, reason="duplicate_from_previous_round")
        accepted = [issue for issue in accepted if issue.id not in known]
        prior_issues = list(state["issues"])
        from app.services.fingerprints import stable_hash
        def claim_key(issue):
            return stable_hash(issue.model_dump(mode="json", exclude={"id", "source_issue_ids", "source_review_unit_ids"}))
        claims = {claim_key(issue): index for index, issue in enumerate(prior_issues)}
        prior_count = len(prior_issues)
        for issue in accepted:
            index = claims.get(claim_key(issue))
            if index is None:
                claims[claim_key(issue)] = len(prior_issues)
                prior_issues.append(issue)
                continue
            canonical = prior_issues[index]
            prior_issues[index] = canonical.model_copy(update={"source_issue_ids": list(dict.fromkeys([
                *canonical.source_issue_ids, canonical.id, *issue.source_issue_ids, issue.id]))})
            audit_issue("deduplication", issue, reason="identical_restored_candidate", canonical_id=canonical.id,
                canonical_review_unit_id=canonical.review_unit_id)
        accepted, prior_issues = prior_issues[prior_count:], prior_issues[:prior_count]
        requested = state.get("next_action")
        if requested is not None:
            try:
                requested = AgentAction.model_validate(requested.model_dump(mode="json"))
            except ValueError:
                return {"pending_issues": [], "issues": [*state["issues"], *accepted],
                        "error": "review_step_next_need_invalid",
                        "next_action": AgentAction(action=AgentActionName.task_done, reason="取证动作未通过 schema 校验")}
        if requested is not None and requested.action not in UNIT_ALLOWED_ACTIONS:
            return {"pending_issues": [], "issues": [*state["issues"], *accepted],
                "error": "review_step_next_need_not_allowed",
                "next_action": AgentAction(action=AgentActionName.task_done,
                    reason="review_step 返回了白名单外动作，拒绝执行并保留未完成状态")}
        followup_actions = {
            AgentActionName.report_issue,
            AgentActionName.file_read,
            AgentActionName.file_find,
            AgentActionName.code_search,
            AgentActionName.file_read_diff,
        }
        # review_step 的 next_need 是下一轮取证义务。收集本轮问题时只清空
        # pending_issues，不能把受控工具动作一起抹掉。
        if requested is not None and requested.action in followup_actions:
            return {
                "next_action": requested,
                "pending_issues": [],
                "issues": [*prior_issues, *accepted],
                "issue_round_completed": False,
                "messages": [*state["messages"], AgentEvent(
                    action=AgentActionName.report_issue,
                    reason="保留 review_step 的受控取证需求，先更新证据再复查",
                    status="completed", message=f"本轮报告 {len(accepted)} 个问题，待取证",
                    review_unit_id=state["unit"].id,
                )],
            }
        return {
            "next_action": None,
            "pending_issues": [],
            "issues": [*prior_issues, *accepted],
            "issue_round_completed": True,
            "messages": [*state["messages"], AgentEvent(
                action=AgentActionName.report_issue,
                reason="执行 Unit 独立审查并收集结构化问题",
                status="completed",
                message=f"本轮报告 {len(accepted)} 个问题",
                review_unit_id=state["unit"].id,
            )],
        }

    async def _finish_unit_node(
        self, state: "_ReviewUnitGraphState"
    ) -> "_ReviewUnitGraphState":
        action = state["next_action"]
        if state.get("error"):
            return {
                "done": True,
                "terminal_reason": (
                    state.get("terminal_reason")
                    or ReviewUnitTerminalReason.execution_error
                ),
                "messages": [*state["messages"], AgentEvent(
                    action=AgentActionName.task_done,
                    reason=action.reason,
                    status="failed",
                    message=state["error"],
                    review_unit_id=state["unit"].id,
                )],
            }
        if state.get("diff_manifest"):
            active = active_evidence_set(state["diff_manifest"], state["unit_files"], state["unit"].id,
                (state["parent_state"].get("unit_diff_batch") or {}).get("ranges"), state["context"])
            ledger = state["coverage_ledger"]
            gaps = coverage_gaps(state["diff_manifest"], ledger,
                active if state["diff_manifest"].review_unit_id != state["unit"].id else None, state.get("unit_plan"))
            update = {"coverage_ledger": ledger, "active_evidence_set": active}
            self._remember_unit_state(update)
            if gaps:
                from app.services.unit_coverage import classified_coverage_gaps
                details = classified_coverage_gaps(state["diff_manifest"], ledger,
                    active if state["diff_manifest"].review_unit_id != state["unit"].id else None, state.get("unit_plan"))
                unsupported = set(unsupported_hunk_ids(state["diff_manifest"], active))
                unchecked_text = any(item.evidence_id and item.manifest_id in gaps for item in active.hunks)
                if (state.get("terminal_reason") != ReviewUnitTerminalReason.no_new_context
                        and not state["issue_round_completed"] and (not unsupported or unchecked_text)
                        and state["budget"].can_consume(model_calls=1, diagnosis_attempts=1)):
                    return {**update, "done": False, "next_action": AgentAction(action="report_issue",
                        reason="覆盖门拒绝提前结束，先检查当前活动证据")}
                previous = state.get("input_coverage")
                summary = state.get("review_summary") or UnitReviewSummary()
                record = summary.record or summary.last_valid_record
                contract_state = contract_coverage_state(
                    [record] if record is not None else [],
                    [item.id for item in summary.evidence],
                    state["unit"],
                    state["diff_manifest"],
                )
                coverage = (previous or UnitInputCoverage()).model_copy(update={"target_coverage":
                    "unknown" if previous and previous.target_coverage == "unknown" else "partial" if details["targets"] else "complete",
                    "contract_coverage": "partial" if details["questions"] or details["dependencies"] else "complete",
                    "contract_evidence_coverage": contract_state["evidence_coverage"],
                    "contract_verification_status": contract_state["verification_status"],
                    "unresolved_questions": details["questions"], "unresolved_dependencies": details["dependencies"],
                    "blocking_dependencies": contract_state["blocking_dependencies"],
                    "reason": previous.reason if previous and previous.reason else "unit_coverage_gate_incomplete", "omitted_targets": list(dict.fromkeys([
                        *(previous.omitted_targets if previous else []), *details["targets"]]))})
                if unsupported:
                    coverage = coverage.model_copy(update={"reason": NO_HUNK_CHANGE_REASON,
                        "evidence_coverage": "partial"})
                return {**update, "done": True, "input_coverage": coverage,
                    "terminal_reason": state.get("terminal_reason") or (ReviewUnitTerminalReason.completed if state["issues"] else ReviewUnitTerminalReason.no_issue),
                    "messages": [*state["messages"], AgentEvent(action="task_done", reason=action.reason,
                        status="failed", message="覆盖门未通过；保留未覆盖项与当前有效记录", review_unit_id=state["unit"].id)]}
            previous = state.get("input_coverage")
            coverage = (previous or UnitInputCoverage(evidence_coverage="complete", target_coverage="complete")).model_copy(
                update={"contract_coverage": "complete", "unresolved_questions": [], "unresolved_dependencies": []})
            return {**update, "done": True, "input_coverage": coverage,
                "terminal_reason": state.get("terminal_reason") or (ReviewUnitTerminalReason.completed if state["issues"] else ReviewUnitTerminalReason.no_issue),
                "messages": [*state["messages"], AgentEvent(action="task_done", reason=action.reason, status="completed",
                    message="活动范围覆盖门通过；检查记录不表示正确性证明", review_unit_id=state["unit"].id)]}
        return {
            "done": True,
            "terminal_reason": (
                state.get("terminal_reason")
                or (
                    ReviewUnitTerminalReason.completed
                    if state["issues"]
                    else ReviewUnitTerminalReason.no_issue
                )
            ),
            "messages": [*state["messages"], AgentEvent(
                action=AgentActionName.task_done,
                reason=action.reason,
                status="completed",
                message="Review Unit 显式结束",
                review_unit_id=state["unit"].id,
            )],
        }

    @staticmethod
    def _budget_for(unit: ReviewUnit, *, workset_count: int = 1) -> ExecutionBudget:
        from app.services.unit_worksets import MAX_UNIT_WORKSETS
        from app.services.review_run_budget import current_run_budget

        if not 1 <= workset_count <= MAX_UNIT_WORKSETS:
            raise ValueError("invalid_unit_workset_count")
        run = current_run_budget()
        if run is not None:
            return ExecutionBudget(max_model_calls=run.max_calls, max_token_usage=run.max_tokens,
                max_diagnosis_attempts=run.max_calls, max_context_retrievals=12, max_patch_attempts=0)
        additional_diagnoses = workset_count - 1
        if unit.complexity == ReviewUnitComplexity.small:
            return ExecutionBudget(
                max_context_retrievals=4,
                max_diagnosis_attempts=1 + additional_diagnoses,
                max_patch_attempts=0,
                max_model_calls=3 + additional_diagnoses,
                max_token_usage=max(12_000, unit.estimated_tokens + 8_192),
            )
        if unit.complexity == ReviewUnitComplexity.medium:
            return ExecutionBudget(
                max_context_retrievals=8,
                max_diagnosis_attempts=2 + additional_diagnoses,
                max_patch_attempts=0,
                max_model_calls=5 + additional_diagnoses,
                max_token_usage=max(24_000, unit.estimated_tokens + 12_000),
            )
        return ExecutionBudget(
            max_context_retrievals=12,
            max_diagnosis_attempts=3 + additional_diagnoses,
            max_patch_attempts=0,
            max_model_calls=7 + additional_diagnoses,
            max_token_usage=max(48_000, unit.estimated_tokens + 20_000),
        )

    def _unit_diff(self, unit: ReviewUnit, by_path: dict[str, ChangedFile]) -> str:
        hunk_ids = {
            path: [
                self.planner.hunk_id(path, index, hunk.model_dump(mode="json"))
                for index, hunk in enumerate(item.hunks)
            ]
            for path, item in by_path.items()
        }
        return self.planner.normalized_unit_diff(unit, by_path, hunk_ids)

    def _unit_changed_files(
        self, unit: ReviewUnit, by_path: dict[str, ChangedFile]
    ) -> list[ChangedFile]:
        selected = set(unit.diff_hunk_ids)
        result: list[ChangedFile] = []
        for path in unit.primary_files:
            item = by_path[path]
            hunks = [
                hunk for index, hunk in enumerate(item.hunks)
                if not selected or self.planner.hunk_id(
                    path, index, hunk.model_dump(mode="json")
                ) in selected
            ]
            result.append(item.model_copy(update={"hunks": hunks}))
        return result

    @staticmethod
    def _unit_state(
        state: dict[str, Any],
        unit: ReviewUnit,
        scope: ReviewToolScope,
        changed_files: list[ChangedFile],
        budget: ExecutionBudget,
        context: list[dict[str, Any]],
        *,
        unit_diff: str = "",
        unit_plan: UnitReviewPlan | None = None,
        review_summary: UnitReviewSummary | None = None,
        latest_attempt: dict[str, Any] | None = None,
        coverage_ledger: UnitCoverageLedger | None = None,
        evidence_store: UnitEvidenceStore | None = None,
    ) -> dict[str, Any]:
        readable = scope.readable_files
        language_context = build_language_context(
            unit.primary_files,
            state.get("file_index") or [],
            state.get("project_meta") or {},
        )

        record_input = build_record_input(changed_files, context, unit_plan,
            str(state.get("head_sha") or ""), review_diff_base(state))
        from app.services.file_change_evidence import set_file_change_impact_scope
        set_file_change_impact_scope(record_input, unit.related_files)
        record_input["pr_intent"] = build_pr_intent(state.get("pr_info"))
        hierarchy = {}
        if state.get("diff_manifest"):
            manifest = UnitDiffManifest.model_validate(state["diff_manifest"])
            record_input["diff_manifest"] = state["diff_manifest"]
            hierarchy = {"diff_manifest": state["diff_manifest"],
                "coverage_ledger": coverage_ledger.model_dump(mode="json") if coverage_ledger else state["coverage_ledger"],
                "unit_metadata": state["unit_metadata"],
                "active_evidence_set": active_evidence_set(manifest, changed_files, unit.id,
                    (state.get("unit_diff_batch") or {}).get("ranges"), context).model_dump(mode="json")}
        prior = review_summary if review_summary and review_summary.last_valid_record else state.get("batch_memory_summary") or review_summary
        memory, restored = build_working_memory(prior, record_input, unit.id, latest_attempt)
        restored_ids = {item["id"] for item in restored}
        return {
            **hierarchy,
            **({"evidence_store_catalog": store_catalog(evidence_store, {item["id"] for item in record_input["evidence"]})}
               if evidence_store is not None else {}),
            "task_id": state.get("task_id"),
            "review_unit_id": unit.id,
            "review_unit": unit.model_dump(mode="json"),
            "review_tool_scope": scope.model_dump(mode="json"),
            "unit_diff": unit_diff,
            "unit_plan": unit_plan.model_dump(mode="json") if unit_plan else None,
            "pr_intent": record_input["pr_intent"],
            "working_memory": memory,
            **(
                {"prior_contract_dependencies": memory["contract_dependencies"]}
                if memory.get("contract_dependencies")
                else {}
            ),
            "memory_evidence": restored,
            "evidence_snapshot": record_input["snapshot"],
            "evidence_catalog": [{key: value for key, value in item.items() if key != "content"}
                                 for item in record_input["evidence"]],
            "project_meta": state.get("project_meta") or {},
            "language_context": language_context,
            "phase": ReviewPhase.discovery,
            "changed_files": [item.model_dump(mode="json") for item in changed_files],
            "file_index": [
                item for item in state.get("file_index") or [] if item.get("path") in readable
            ],
            "symbol_index": [
                item for item in state.get("symbol_index") or [] if item.get("file") in readable
            ],
            "context_provenance": [
                item.model_dump(mode="json") for item in unit.context_provenance
            ],
            "context_snippets": [item for item in record_input["readonly_context"]
                                 if item["evidence_id"] not in restored_ids],
            "retrieval_history": [],
            "execution_budget": budget.model_dump(),
        }

    @staticmethod
    def _retrieval_next_action(state, budget, admission):
        if not admission["omitted"] and (admission.get("after") or {}).get("input_mode") != "core":
            return None
        completed = state.get("issue_round_completed") and not admission["accepted_chunks"]
        action = (AgentActionName.task_done if completed and not budget.can_consume(
            diagnosis_attempts=1, model_calls=1) else AgentActionName.report_issue)
        return AgentAction(action=action, reason="检索工作集达到诊断预算边界，保留省略范围并收敛审查")

    def _probe_diagnosis_admission(self, state):
        """不消费额度；与最终诊断共用完整请求、降级选择和模型计数路径。"""
        from app.agents.providers import LLMProviderError
        from app.services.review_admission import AdmissionKind, annotate_admission, budget_fit

        budget = state["budget"]
        capacity = {"remaining_tokens": max(0, budget.max_token_usage - budget.token_usage),
                    "remaining_calls": max(0, budget.max_model_calls - budget.model_calls),
                    "remaining_diagnosis_attempts": max(0, budget.max_diagnosis_attempts - budget.diagnosis_attempts)}
        if not budget.can_consume(diagnosis_attempts=1, model_calls=1):
            fit = budget_fit(budget, model_calls=1, diagnosis_attempts=1)
            return annotate_admission({**capacity}, admitted=False,
                                      reason="unit_diagnosis_budget_exhausted", budget=fit,
                                      kind=AdmissionKind.BUDGET, can_defer=True, can_split=False)
        try:
            args, degradation = self._select_diagnosis_input(state)
            estimate = self._diagnosis_estimate(args)
        except LLMProviderError as exc:
            reason, _, metadata = str(exc).partition(":")
            if reason not in {"model_context_window_exceeded", "model_input_limit_exceeded",
                              "model_output_limit_exceeded", "required_input_too_large"}:
                raise
            try:
                estimate = json.loads(metadata)
            except (ValueError, TypeError):
                estimate = {}
            return annotate_admission({**estimate, **capacity}, admitted=False,
                                      reason=reason, kind=AdmissionKind.REQUEST_FIT,
                                      can_defer=False, can_split=True)
        admitted = budget.can_consume(model_calls=1, token_usage=estimate["reserved_tokens"])
        base = {**estimate, **capacity, "input_mode": "core" if degradation else "full"}
        if admitted:
            return annotate_admission(base, admitted=True,
                                      budget=budget_fit(budget, model_calls=1,
                                                        token_usage=estimate["reserved_tokens"]),
                                      kind=AdmissionKind.BUDGET, can_defer=False, can_split=False)
        return annotate_admission(base, admitted=False,
                                  reason="unit_request_budget_exhausted",
                                  budget=budget_fit(budget, model_calls=1,
                                                    token_usage=estimate["reserved_tokens"]),
                                  kind=AdmissionKind.BUDGET, can_defer=True, can_split=False)

    def _admit_retrieved_context(self, state, candidates, budget, *, request_key=None):
        """归档合法完整块；高价值活动证据可替换低价值块，最终准入失败则回滚可见集。"""
        parent = state["parent_state"]
        head, base = str(parent.get("head_sha") or ""), review_diff_base(parent)
        current, _ = build_context_evidence(state["context"], head, base)
        manifest = state.get("diff_manifest")
        root_id = manifest.review_unit_id if manifest else state["unit"].id
        store = restore_store(state.get("evidence_store"), root_id, evidence_snapshot(head, base), state["scope"].readable_files)
        put_chunks(store, current)
        active_ids = {item["evidence_id"] for item in current}
        seen = set(active_ids)
        chunks, omitted = [], []
        for candidate in candidates:
            try:
                if candidate.get("start_line") is not None and int(candidate["start_line"]) < 1:
                    raise ValueError("context_source_line_must_be_positive")
                if candidate.get("file") not in state["scope"].readable_files or is_sensitive_repository_change(str(candidate.get("file") or "")):
                    raise ValueError("context_outside_readable_scope")
                pieces, _ = build_context_evidence([candidate], head, base)
                for piece in pieces:
                    ContextSnippet.model_validate({"relevance": "exploratory", **piece})
                chunks.extend(pieces)
            except ValueError as exc:
                coordinates = {}
                for name in ("start_line", "end_line"):
                    try:
                        coordinates[name] = int(candidate.get(name) or 0)
                    except (ValueError, TypeError):
                        coordinates[name] = 0
                omitted.append(UnitContextOmission(file_path=str(candidate.get("file") or ""),
                    **coordinates,
                    content_hash=hashlib.sha256(str(candidate.get("content") or "").encode("utf-8")).hexdigest(),
                    reason="invalid_context:" + ("invalid_context_metadata" if hasattr(exc, "errors") else str(exc))).model_dump(mode="json"))
        previous_ids = set(store.entries)
        put_chunks(store, chunks, request_key)
        priorities, pinned = evidence_priorities(state, store)
        inventory = store_chunks(store)
        # 已归档的关键引用可离线恢复；未引用的归档正文不会自动注入模型。
        for identity in sorted(pinned - active_ids):
            chunks.append(inventory[identity])
        trial = {**state, "budget": budget, "context": current}
        before = self._probe_diagnosis_admission(trial) if chunks or omitted else None
        active, duplicates = list(current), 0
        for chunk in sorted(chunks, key=lambda item: (priorities[item["evidence_id"]], store.entries[item["evidence_id"]].discovered_order)):
            if chunk["evidence_id"] in seen:
                duplicates += 1
                continue
            seen.add(chunk["evidence_id"])
            proposed = [*active, chunk]
            victims = sorted((item for item in active if priorities[item["evidence_id"]] > priorities[chunk["evidence_id"]]
                and item["evidence_id"] not in pinned), key=lambda item: (-priorities[item["evidence_id"]],
                    store.entries[item["evidence_id"]].discovered_order))
            while True:
                if sum(len(item["content"]) for item in proposed) > state["scope"].max_context_chars:
                    estimate = {"admitted": False, "reason": "unit_context_char_budget_exhausted"}
                else:
                    estimate = self._probe_diagnosis_admission({**trial, "context": proposed})
                if estimate["admitted"] or not victims:
                    break
                victim = victims.pop(0)
                proposed = [item for item in proposed if item["evidence_id"] != victim["evidence_id"]]
            if estimate["admitted"]:
                active = proposed
            else:
                if chunk["evidence_id"] in previous_ids and request_key is None and parent.get("batch_evidence_store") and chunk["evidence_id"] not in pinned:
                    continue  # 已展示的批次背景可继续归档；关键引用仍必须准入。
                omitted.append(UnitContextOmission(file_path=chunk["file"], start_line=chunk["start_line"],
                    end_line=chunk["end_line"], content_hash=chunk["content_hash"],
                    reason=estimate["reason"], diagnosis_admission=estimate).model_dump(mode="json"))
        accepted = [item for item in active if item["evidence_id"] not in active_ids]
        after = self._probe_diagnosis_admission({**trial, "context": active}) if chunks or omitted else None
        if accepted and not after["admitted"]:
            for chunk in accepted:
                omitted.append(UnitContextOmission(file_path=chunk["file"], start_line=chunk["start_line"],
                    end_line=chunk["end_line"], content_hash=chunk["content_hash"],
                    reason=after["reason"], diagnosis_admission=after).model_dump(mode="json"))
            accepted = []
            active = current
            after = self._probe_diagnosis_admission(trial)
        final_ids = [item["evidence_id"] for item in active]
        # 同一准入内先接纳、后被更高价值块替换的正文也仅归档，不丢失。
        admitted_ids = {item["evidence_id"] for item in accepted}
        omitted = [item for item in omitted if (item["file_path"], item["start_line"], item["end_line"], item["content_hash"])
            not in {(chunk["file"], chunk["start_line"], chunk["end_line"], chunk["content_hash"]) for chunk in accepted}]
        store.revision += 1
        rejected_status = ("budget_rejected" if any(not item["reason"].startswith("invalid_context:")
                           for item in omitted) else "rejected")
        metadata = {"status": "partial" if accepted and omitted else rejected_status if omitted else "completed",
                    "source_snippets": len(candidates), "accepted_chunks": len(accepted),
                    "duplicate_chunks": duplicates, "omitted": omitted, "before": before, "after": after,
                    "revision": store.revision, "stored_count": len(store.entries), "active_evidence_ids": final_ids,
                    "admitted_evidence_ids": [item["evidence_id"] for item in accepted],
                    "evicted_evidence_ids": [item["evidence_id"] for item in current if item["evidence_id"] not in final_ids],
                    "reactivated_evidence_ids": sorted(admitted_ids & previous_ids),
                    "pinned_evidence_ids": sorted(pinned),
                    "priorities": {identity: ("pinned", "high", "medium", "low")[priorities[identity]] for identity in final_ids},
                    "active_scopes": [{"file_path": item["file"], "start_line": item["start_line"], "end_line": item["end_line"],
                                       "content_hash": item["content_hash"]} for item in active],
                    "active_set_changed": final_ids != [item["evidence_id"] for item in current]}
        return accepted, ContextAdmission(metadata, store, active)

    @staticmethod
    def _context_selection_update(state, admission):
        update = {"context": admission.active, "evidence_store": admission.store,
            "context_selection": UnitContextSelection(**{key: admission[key] for key in UnitContextSelection.model_fields})}
        if state.get("diff_manifest"):
            update["active_evidence_set"] = active_evidence_set(state["diff_manifest"], state["unit_files"], state["unit"].id,
                (state["parent_state"].get("unit_diff_batch") or {}).get("ranges"), admission.active)
        ReviewUnitExecutor._remember_unit_state(update)
        return update

    @staticmethod
    def _cached_context(state, request_key, file_request=None):
        if state.get("evidence_store") is None:
            return []
        parent = state["parent_state"]
        snapshot = evidence_snapshot(str(parent.get("head_sha") or ""), review_diff_base(parent))
        manifest = state.get("diff_manifest")
        store = restore_store(state["evidence_store"], manifest.review_unit_id if manifest else state["unit"].id,
                              snapshot, state["scope"].readable_files)
        _, references = build_context_evidence(state["context"], snapshot["head_sha"], snapshot["base_sha"])
        active_ids = {item["id"] for item in references}
        chunks = archived_request_chunks(store, request_key, active_ids)
        if file_request:
            try:
                request = FileReadRequest.model_validate(file_request)
            except ValueError:
                return []
            end_line = request.end_line or request.start_line + state["scope"].max_lines_per_read - 1
            if end_line - request.start_line + 1 > state["scope"].max_lines_per_read:
                return []
            inventory = store_chunks(store)
            expected_group = store.request_groups.get(request_key)
            if expected_group is not None and any(identity not in inventory for identity in expected_group):
                return []  # 原请求的一块失效不能被冒充为 EOF clamp。
            same_request = [inventory[identity] for identity, item in store.entries.items() if request_key in item.request_fingerprints
                and item.snippet.file == request.file_path and request.start_line <= item.snippet.start_line <= item.snippet.end_line <= end_line]
            if same_request:
                cursor = request.start_line
                for item in sorted(same_request, key=lambda chunk: chunk["start_line"]):
                    if item["start_line"] != cursor:
                        return []
                    cursor = item["end_line"] + 1
                if cursor <= end_line and any(item.get("truncated") for item in same_request):
                    return []  # 内容/行数上限截断不是 EOF，不能缓存成完整请求。
                # 同快照已成功读取过此请求，允许完整块集合及原有 EOF clamp。
                chunks = [item for item in sorted(same_request, key=lambda item: item["start_line"])
                          if item["evidence_id"] not in active_ids]
            else:
                candidates = [chunk for chunk in inventory.values() if chunk["file"] == request.file_path
                    and request.start_line <= chunk["start_line"] <= chunk["end_line"] <= end_line]
                cursor, selected = request.start_line, []
                while cursor <= end_line:
                    next_chunk = max((chunk for chunk in candidates if chunk["start_line"] == cursor),
                                     key=lambda chunk: chunk["end_line"], default=None)
                    if next_chunk is None:
                        return []
                    selected.append(next_chunk)
                    cursor = next_chunk["end_line"] + 1
                chunks = [chunk for chunk in selected if chunk["evidence_id"] not in active_ids]
        if not file_request and len(chunks) > state["scope"].max_search_results:
            return []
        return [{**chunk, "relevance": "direct" if file_request else chunk["relevance"]}
            for chunk in chunks if chunk["end_line"] - chunk["start_line"] + 1 <= state["scope"].max_lines_per_read]

    @staticmethod
    def _retrieval_input_coverage(state, admission):
        previous = state.get("input_coverage")
        old_omissions = previous.omitted_context if previous else []
        scopes = {(item["file_path"], item["start_line"], item["end_line"], item["content_hash"])
                  for item in admission.get("active_scopes", [])}
        retained = [item for item in old_omissions if (item.file_path, item.start_line, item.end_line, item.content_hash) not in scopes]
        if not admission["omitted"] and len(retained) == len(old_omissions):
            return previous
        omissions = [*retained,
                     *(UnitContextOmission.model_validate(item) for item in admission["omitted"])]
        by_scope = {(item.file_path, item.start_line, item.end_line, item.content_hash): item for item in omissions}
        resolved_labels = {f"读取并检查 {item.file_path}:{item.start_line}-{item.end_line}（{item.reason}）" for item in old_omissions if item not in retained}
        old_targets = [item for item in previous.omitted_targets if item not in resolved_labels] if previous else []
        targets = [*old_targets,
                   *(f"读取并检查 {item.file_path}:{item.start_line}-{item.end_line}（{item.reason}）" for item in by_scope.values())]
        coverage = (previous or UnitInputCoverage()).model_copy(update={
            "evidence_coverage": "partial" if by_scope else "complete",
            "target_coverage": "unknown" if by_scope else "partial" if previous and previous.omitted_components else "complete",
            "reason": "retrieval_context_not_admitted" if by_scope else DIAGNOSIS_BACKGROUND_DEGRADED if previous and previous.omitted_components else None,
            "omitted_context": list(by_scope.values()),
            "omitted_targets": list(dict.fromkeys(targets)),
        })
        ReviewUnitExecutor._remember_unit_state({"input_coverage": coverage})
        return coverage

    @staticmethod
    def _fit_context_budget(
        current: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
        max_chars: int,
    ) -> list[dict[str, Any]]:
        """按稳定顺序截取新上下文，保证 Unit 总字符预算是硬限制。"""
        remaining = max_chars - sum(len(str(item.get("content") or "")) for item in current)
        accepted: list[dict[str, Any]] = []
        for item in candidates:
            if remaining <= 0:
                break
            content = str(item.get("content") or "")
            if item.get("source") == "file_read_diff":
                continue  # Current canonical hunks already provide both sides.
            if content.endswith("\n...(truncated)"):
                # Legacy previews may end in a partial line; they need a bounded reread.
                continue
            bounded = complete_line_prefix(content, remaining)
            if not bounded:
                continue
            start = int(item.get("start_line") or 1)
            item = {**item, "content": bounded,
                    "truncated": bool(item.get("truncated")) or len(bounded) < len(content),
                    "requested_end_line": item.get("requested_end_line") or item.get("end_line"),
                    "end_line": start + len(bounded.splitlines()) - 1}
            accepted.append(item)
            remaining -= len(str(item.get("content") or ""))
        return accepted

    @staticmethod
    def _enhanced_diff(
        unit_diff: str,
        context: list[dict[str, Any]],
        unit_plan: UnitReviewPlan | None = None,
        language_context: dict[str, Any] | None = None,
    ) -> str:
        sections: list[str] = []
        rendered_rules = render_language_rule_context(language_context or {})
        if rendered_rules:
            sections.append(rendered_rules)
        if unit_plan is not None:
            sections.extend([
                "## Unit review plan guidance",
                "The following risk hypotheses are unconfirmed guidance, not established issues. "
                "Independently verify them against code evidence. The plan is not exhaustive; report "
                "clear defects outside it when found.",
                unit_plan.model_dump_json(),
            ])
        if context:
            sections.append("## Unit scoped context")
        for snippet in context:
            provenance = snippet.get("why_retrieved")
            sections.append(
                f"### {snippet.get('file')}:{snippet.get('start_line')}-{snippet.get('end_line')}"
            )
            if provenance:
                sections.append(
                    f"Retrieved via {snippet.get('source')} (distance={snippet.get('distance')}, "
                    f"confidence={snippet.get('confidence')}): {provenance}"
                )
            sections.append(
                f"```{markdown_language_for_path(snippet.get('file', ''))}"
            )
            sections.append(snippet.get("content", ""))
            sections.append("```")
        sections.extend(["## Unit diff", unit_diff])
        return "\n".join(sections)

    @staticmethod
    def _validate_unit_plan_scope(
        plan: UnitReviewPlan,
        scope: ReviewToolScope,
        symbol_index: list[dict[str, Any]],
    ) -> None:
        if plan.initial_action.action not in UNIT_ALLOWED_ACTIONS:
            raise ValueError("Unit Plan initial action is outside the Unit action allowlist")
        readable = scope.readable_files
        indexed_symbols = {item.get("symbol") for item in symbol_index}
        retrieval_plans = [
            suggestion
            for hypothesis in plan.risk_hypotheses
            for suggestion in hypothesis.retrieval_suggestions
        ]
        for hypothesis in plan.risk_hypotheses:
            unknown_files = set(hypothesis.affected_files) - readable
            if unknown_files:
                raise ValueError(
                    f"Unit Plan hypothesis references files outside review scope: {sorted(unknown_files)}"
                )
            unknown_symbols = set(hypothesis.affected_symbols) - indexed_symbols
            if unknown_symbols:
                raise ValueError(
                    f"Unit Plan hypothesis references symbols outside review scope: {sorted(unknown_symbols)}"
                )
        if plan.initial_action.action == AgentActionName.retrieve_context:
            retrieval_plans.append(ContextRetrievalPlan.model_validate(
                plan.initial_action.tool_args["plan"]
            ))
        elif plan.initial_action.action == AgentActionName.file_read:
            request = FileReadRequest.model_validate(plan.initial_action.tool_args["request"])
            if request.file_path not in readable:
                raise ValueError("Unit Plan file_read references a file outside review scope")
        elif plan.initial_action.action == AgentActionName.file_read_diff:
            request = FileReadDiffRequest.model_validate(plan.initial_action.tool_args["request"])
            if request.file_path not in scope.commentable_files:
                raise ValueError("Unit Plan file_read_diff references a non-commentable file")
        elif plan.initial_action.action == AgentActionName.file_find:
            request = FileFindRequest.model_validate(plan.initial_action.tool_args["request"])
            if request.max_results > scope.max_search_results:
                raise ValueError("Unit Plan file_find exceeds scope result limit")
        for retrieval in retrieval_plans:
            unknown_files = set(retrieval.target_files) - readable
            if unknown_files:
                raise ValueError(
                    f"Unit Plan retrieval references files outside review scope: {sorted(unknown_files)}"
                )
            unknown_symbols = set(retrieval.target_symbols) - indexed_symbols
            if unknown_symbols:
                raise ValueError(
                    f"Unit Plan retrieval references symbols outside review scope: {sorted(unknown_symbols)}"
                )
            if retrieval.max_results > scope.max_search_results:
                raise ValueError("Unit Plan retrieval exceeds scope result limit")

    @staticmethod
    def _filter_issues(
        model_issues: list[ReviewIssue],
        unit: ReviewUnit,
        scope: ReviewToolScope,
    ) -> list[ReviewIssue]:
        from app.review.issue_audit import audit_issue

        accepted: list[ReviewIssue] = []
        seen: set[str] = set()
        for issue in model_issues:
            issue = issue.for_review_unit(unit.id)
            if issue.id in seen:
                audit_issue("unit_filter", issue, reason="duplicate_issue_id")
                continue
            seen.add(issue.id)
            if issue.primary_evidence.file_path not in scope.commentable_files:
                audit_issue("unit_filter", issue, reason="primary_file_out_of_scope")
                continue
            if any(
                anchor.file_path not in scope.readable_files
                for anchor in issue.supporting_evidence
            ):
                audit_issue("unit_filter", issue, reason="supporting_file_out_of_scope")
                continue
            audit_issue("unit_filter", issue, reason="accepted")
            accepted.append(issue)
        return accepted

    @staticmethod
    def _event(
        unit_id: str, action: AgentAction, status: str, message: str
    ) -> AgentEvent:
        return AgentEvent(
            action=action.action,
            reason=action.reason,
            status=status,
            message=message,
            review_unit_id=unit_id,
            created_at=datetime.now(timezone.utc),
        )
