"""Review Unit 独立执行与有界并发调度。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.providers import LLMProvider
from app.services.review_input_context import build_pr_intent, build_working_memory, complete_line_prefix
from app.review.input_protocol import CANONICAL_UNIT_INPUT_PROTOCOL, legacy_unit_input_allowed
from app.review.unit_completion import DIAGNOSIS_BACKGROUND_DEGRADED, review_unit_input_coverage
from app.services.fingerprints import unit_execution_fingerprint
from app.models.review import (
    AgentAction,
    AgentActionName,
    AgentEvent,
    ChangedFile,
    ContextRetrievalPlan,
    CodeSearchRequest,
    ContextSnippet,
    ExecutionBudget,
    FileFindRequest,
    FileReadDiffRequest,
    FileReadRequest,
    HumanReviewRequest,
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
)
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
    needs_human: bool
    human_request: HumanReviewRequest | None
    terminal_reason: ReviewUnitTerminalReason | None
    review_summary: UnitReviewSummary
    last_valid_review_summary: UnitReviewSummary | None
    latest_review_attempt: dict[str, Any]
    input_coverage: UnitInputCoverage | None


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

    async def execute(
        self,
        units: list[ReviewUnit],
        state: dict[str, Any],
    ) -> list[ReviewUnitResult]:
        if not units:
            return []
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
        return [result for result in results if result is not None]

    async def execute_unit(
        self,
        unit: ReviewUnit,
        state: dict[str, Any],
    ) -> ReviewUnitResult:
        from app.services.model_request_budgeter import unit_budget_snapshot

        snapshot = {"budget": self._budget_for(unit), "input_fingerprint": unit_execution_fingerprint(
            unit.fingerprint, state, self.provider, self.input_mode)}
        snapshot_token = unit_budget_snapshot.set(snapshot)
        try:
            self._check_provider_protocol()
            async with asyncio.timeout(self.timeout_seconds):
                return await self._execute_unit(unit, state)
        except TimeoutError:
            return ReviewUnitResult(
                review_unit_id=unit.id,
                input_fingerprint=snapshot["input_fingerprint"],
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
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            from app.agents.providers import LLMProviderError
            from app.services.model_usage import annotate_usage

            usage = exc.usage if isinstance(exc, LLMProviderError) else None
            usage = annotate_usage(
                usage,
                review_unit_id=unit.id,
                unit_complexity=unit.complexity,
            )
            return ReviewUnitResult(
                review_unit_id=unit.id,
                input_fingerprint=snapshot["input_fingerprint"],
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
        followup = state.get("cross_unit_followup")
        followup_plan = None
        if followup:
            # 补查复用只读子图，但禁止发现范围扩张，并跳过二次规划。
            scope = scope.model_copy(update={
                "readable_files": set(unit.primary_files) | set(unit.related_files),
                "repository_discovery_enabled": False,
                "max_context_chars": 12_000,
            })
            budget = ExecutionBudget(max_model_calls=3, max_token_usage=12_000,
                                     max_diagnosis_attempts=1, max_context_retrievals=2,
                                     max_patch_attempts=0)
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
            "unit_plan": followup_plan,
            "plan_status": UnitPlanStatus.skipped if skip_plan else UnitPlanStatus.failed,
            "plan_skip_reason": "small_low_risk_unit" if skip_plan else None,
            "plan_error": None,
            "context": self._fit_context_budget([], [
                item for item in state.get("cross_unit_followup_context") or []
                if item.get("file") in scope.readable_files
            ], scope.max_context_chars) if followup else [],
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
        }
        from app.services.model_request_budgeter import unit_budget_snapshot

        observer = unit_budget_snapshot.get()
        binding = observer["input_fingerprint"] if observer else unit_execution_fingerprint(
            unit.fingerprint, state, self.provider, self.input_mode)
        prior = next((ReviewUnitResult.model_validate(raw) for raw in state.get("review_unit_results") or []
                      if raw.get("review_unit_id") == unit.id and raw.get("input_fingerprint") == binding), None)
        if prior and self.input_mode == "canonical":
            restored_context = self._fit_context_budget([], [item.model_dump(mode="json") for item in prior.context_snippets
                                                            if item.file in scope.readable_files], scope.max_context_chars)
            restored_plan = followup_plan or prior.plan
            request = build_record_input(unit_files, restored_context, restored_plan,
                str(state.get("head_sha") or ""), str(state.get("base_sha") or ""))
            request["pr_intent"] = build_pr_intent(state.get("pr_info"))
            recovered = merge_review_summaries(prior.review_summary, validate_record(None, request, scope.readable_files), None)
            if recovered.last_valid_record:
                recovered = recovered.model_copy(update={"record": prior.review_summary.record,
                    "status": prior.review_summary.status, "latest_attempt_status": prior.review_summary.latest_attempt_status,
                    "latest_attempt_reason": prior.review_summary.latest_attempt_reason, "reason": prior.review_summary.reason})
            graph_state.update(context=restored_context, unit_plan=restored_plan,
                skip_plan=True if restored_plan else skip_plan, budget=prior.execution_budget,
                plan_status=prior.plan_status if restored_plan else graph_state["plan_status"],
                plan_skip_reason="restored_plan" if restored_plan else graph_state["plan_skip_reason"],
                review_summary=recovered, last_valid_review_summary=recovered if recovered.last_valid_record else None,
                latest_review_attempt={"status": recovered.latest_attempt_status or recovered.status, "reason": recovered.reason},
                input_coverage=review_unit_input_coverage(prior),
                model_usages=[item.model_dump(mode="json") for item in prior.model_usages])
        self._remember_unit_state(graph_state)
        config = None
        if getattr(self.unit_graph, "checkpointer", None) not in (None, False):
            config = unit_thread_config(str(state.get("task_id") or "unknown"), unit.id)
        result = await self.unit_graph.ainvoke(graph_state, config=config)
        if result.get("needs_human"):
            terminal_reason = ReviewUnitTerminalReason.human_required
        elif result.get("done") and not result.get("error"):
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
                ReviewUnitStatus.needs_human
                if result.get("needs_human")
                else ReviewUnitStatus.completed
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
            context_snippets=[
                ContextSnippet.model_validate(item) for item in result.get("context") or []
            ],
            messages=result.get("messages") or [],
            tool_events=result.get("tool_events") or [],
            execution_budget=result.get("budget") or budget,
            model_usages=result.get("model_usages") or [],
            error=result.get("error"),
            human_request=result.get("human_request"),
        )

    def _build_unit_graph(self) -> StateGraph:
        """构造每个 Review Unit 独立运行的有界 LangGraph 子图。"""
        graph = StateGraph(_ReviewUnitGraphState)
        graph.add_node("prepare_unit", self._prepare_unit_node)
        graph.add_node("plan_unit", self._plan_unit_node)
        graph.add_node("agent_decide", self._agent_decide_node)
        graph.add_node("execute_read_tool", self._execute_read_tool_node)
        graph.add_node("report_issue", self._report_issue_node)
        graph.add_node("collect_issue", self._collect_issue_node)
        graph.add_node("finish_unit", self._finish_unit_node)
        graph.add_edge(START, "prepare_unit")
        graph.add_conditional_edges(
            "prepare_unit",
            lambda state: "agent_decide" if state["skip_plan"] else "plan_unit",
            {"plan_unit": "plan_unit", "agent_decide": "agent_decide"},
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
        graph.add_edge("finish_unit", END)
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
        if pending is not None:
            return {"next_action": pending}
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
        if not budget.can_consume(model_calls=1):
            issue_round_completed = bool(state.get("issue_round_completed"))
            if issue_round_completed:
                terminal_reason = (
                    ReviewUnitTerminalReason.completed
                    if state.get("issues")
                    else ReviewUnitTerminalReason.no_issue
                )
                return (
                    AgentAction(
                        action="task_done",
                        reason="已完成至少一轮结构化审查，在模型预算边界确定性结束 Unit",
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
            action = AgentAction(action=AgentActionName.task_done, reason="Unit 动作不在只读白名单")
        return action, budget, legacy_review, model_usages, None

    async def _call_unit_model(self, budget, payload, output_tokens, invoke, *, holdback=None):
        from app.agents.providers import LLMProviderError
        from app.services.model_request_budgeter import UnitRequestLedger
        from app.services.model_usage import unpack_model_call

        ledger = UnitRequestLedger(budget, holdback=holdback)
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
            if ledger.reserved_tokens:
                ledger.settle(usage)
            if usage is not None and ledger.reserved_tokens:
                usage.accounted_tokens_estimate = ledger.reserved_tokens
            return result, ledger.budget
        except Exception as exc:
            usage = getattr(exc, "usage", None)
            if ledger.reserved_tokens:
                ledger.settle(usage)
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
            legacy_unit_input_allowed.reset(legacy_token)
            from app.services.model_request_budgeter import unit_budget_snapshot

            snapshot = unit_budget_snapshot.get()
            if snapshot is not None:
                snapshot["budget"] = ledger.budget

    def _check_provider_protocol(self):
        from app.agents.providers import LLMProviderError

        method = getattr(self.provider, "review_unit", None)
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
        if any(item.get("plan") == fingerprint for item in history):
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
            state, plan, budget, events, history, fingerprint
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
        if not budget.can_consume(context_retrievals=1):
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
        if any(item.get("plan") == fingerprint for item in history):
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
        try:
            if action.action == AgentActionName.file_read:
                request = FileReadRequest.model_validate(request_payload)
                snippets = [await context_tool.file_read(
                    scope=state["scope"],
                    file_path=request.file_path,
                    start_line=request.start_line,
                    end_line=request.end_line,
                )]
            elif action.action == AgentActionName.file_find:
                request = FileFindRequest.model_validate(request_payload)
                matches = await context_tool.file_find(
                    scope=state["scope"], query=request.query, max_results=request.max_results
                )
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
                snippets = await CodeSearchTool().retrieve_context(
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
                snippets = [await context_tool.file_read_diff(
                    scope=state["scope"],
                    file_path=request.file_path,
                    changed_files=state["unit_files"],
                    hunk_ids=request.hunk_ids,
                )]
            else:
                raise ValueError(f"unsupported Unit read action: {tool_name}")

            existing = {
                (item.get("file"), item.get("start_line"), item.get("end_line"), item.get("source"))
                for item in state["context"]
            }
            new_items = [
                item for item in snippets
                if (item.get("file"), item.get("start_line"), item.get("end_line"), item.get("source"))
                not in existing
            ]
            new_items = self._fit_context_budget(
                state["context"], new_items, state["scope"].max_context_chars
            )
            result_count = len(matches) if action.action == AgentActionName.file_find else len(new_items)
            events.append(ReviewUnitToolEvent(
                review_unit_id=state["unit"].id,
                tool=tool_name,
                status="completed",
                result_count=result_count,
            ))
            history_item: dict[str, Any] = {
                "plan": fingerprint,
                "result_count": result_count,
                "new_snippet_count": len(new_items),
                "status": "completed",
            }
            if matches:
                history_item["matches"] = matches
            return {
                "next_action": None,
                "budget": budget,
                "context": [*state["context"], *new_items],
                "retrieval_history": [*history, history_item],
                "retrieval_no_new_rounds": (
                    0 if result_count else state["retrieval_no_new_rounds"] + 1
                ),
                "tool_events": events,
            }
        except ValueError as exc:
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
    ) -> "_ReviewUnitGraphState":
        budget = budget.consume(context_retrievals=1)
        try:
            snippets = await CodeSearchTool().retrieve_context(
                changed_files=[item.model_dump(mode="json") for item in state["unit_files"]],
                symbol_index=state["parent_state"].get("symbol_index") or [],
                file_index=state["parent_state"].get("file_index") or [],
                repo_path=state["parent_state"].get("repo_path", ""),
                plan=plan,
                scope=state["scope"],
                repository_graph=state["parent_state"].get("repository_graph") or {},
            )
            existing = {
                (item.get("file"), item.get("start_line"), item.get("end_line"))
                for item in state["context"]
            }
            new_items = [
                item for item in snippets
                if (item.get("file"), item.get("start_line"), item.get("end_line")) not in existing
            ]
            new_items = self._fit_context_budget(
                state["context"], new_items, state["scope"].max_context_chars
            )
            events.append(ReviewUnitToolEvent(
                review_unit_id=state["unit"].id,
                tool="code_search",
                status="completed",
                result_count=len(new_items),
            ))
            history.append({
                "plan": fingerprint,
                "result_count": len(snippets),
                "new_snippet_count": len(new_items),
                "truncated_count": sum(
                    1 for item in snippets
                    if item.get("content", "").endswith("...(truncated)")
                ),
                "status": "completed",
            })
            return {
                "next_action": None,
                "budget": budget,
                "context": [*state["context"], *new_items],
                "retrieval_history": history,
                "retrieval_no_new_rounds": (
                    0 if new_items else state["retrieval_no_new_rounds"] + 1
                ),
                "tool_events": events,
            }
        except ValueError as exc:
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
        if not budget.can_consume(diagnosis_attempts=1, model_calls=1):
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
                    evidence_coverage="complete", reason=DIAGNOSIS_BACKGROUND_DEGRADED,
                    omitted_components=degradation["omitted"], omitted_targets=omitted_targets,
                    omitted_plan=state.get("unit_plan") or (input_coverage.omitted_plan if input_coverage else None),
                )
                self._remember_unit_state({"input_coverage": input_coverage})
            record_input = args[-1]
            with issue_audit_unit(state["unit"].id):
                raw_result, budget = await self._call_unit_model(
                    budget, record_input, 4_096, lambda: self.provider.review_unit(*args),
                )
        except LLMProviderError as exc:
            usage = annotate_usage(
                exc.usage,
                accounted_tokens_estimate=exc.usage.accounted_tokens_estimate if exc.usage else None,
                review_unit_id=state["unit"].id, unit_complexity=state["unit"].complexity,
            )
            failed_summary = validate_record(None, record_input, state["scope"].readable_files, str(exc))
            failed_summary = failed_summary.model_copy(update={"latest_attempt_status": "failed"})
            failed_summary = merge_review_summaries(state.get("review_summary") or UnitReviewSummary(), failed_summary, None)
            if observer is not None:
                observer.update(review_summary=failed_summary, diagnosis_pending=False)
            return {
                "review_summary": failed_summary,
                "pending_issues": [], "next_action": None,
                "budget": getattr(exc, "execution_budget", budget), "error": str(exc),
                "terminal_reason": (ReviewUnitTerminalReason.diagnosis_budget_exhausted
                                    if "unit_request_budget_exhausted" in str(exc)
                                    else ReviewUnitTerminalReason.provider_error),
                "model_usages": append_usage(state.get("model_usages") or [], usage),
                "latest_review_attempt": {"status": "failed", "reason": str(exc)},
                "input_coverage": input_coverage,
            }
        response, usage = unpack_model_call(raw_result)
        if self.input_mode == "legacy":
            response = response.model_copy(update={"review_record": None,
                "record_error": "legacy_provider_without_canonical_evidence_protocol"})
        from app.review.issue_audit import audit_issue

        for issue in response.issues:
            audit_issue("generated", issue.model_copy(update={"review_unit_id": state["unit"].id}))
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
        if degradation and summary.status == "reported":
            # 核心证据诊断可产生候选，但省略的检查目标/工作记忆不能被宣称为完整覆盖。
            summary = summary.model_copy(update={"status": "unknown", "record": None,
                "latest_attempt_status": "unknown", "latest_attempt_reason": "diagnosis_background_budget_degraded",
                "reason": "diagnosis_background_budget_degraded"})
            latest_attempt = {"status": "unknown", "reason": summary.reason}
            input_coverage = input_coverage.model_copy(update={"target_coverage": "partial"})
        usage = annotate_usage(
            usage,
            accounted_tokens_estimate=usage.accounted_tokens_estimate if usage else None,
            review_unit_id=state["unit"].id,
            unit_complexity=state["unit"].complexity,
        )
        self._remember_unit_state({"review_summary": summary,
            "input_coverage": input_coverage,
            **({"unit_plan": None, "plan_status": UnitPlanStatus.skipped} if degradation else {}),
            "model_usages": append_usage(state.get("model_usages") or [], usage)})
        if observer is not None:
            observer["diagnosis_pending"] = False
        return {
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
            "model_usages": append_usage(state.get("model_usages") or [], usage),
        }

    def _diagnosis_args(self, state, *, core=False):
        pr = PullRequestInfo.model_validate(state["parent_state"].get("pr_info") or {})
        plan = None if core else state.get("unit_plan")
        record_input = build_record_input(state["unit_files"], state["context"], plan,
            str(state["parent_state"].get("head_sha") or ""), str(state["parent_state"].get("base_sha") or ""))
        record_input["pr_intent"] = build_pr_intent(pr)
        record_input["input_protocol"] = CANONICAL_UNIT_INPUT_PROTOCOL if self.input_mode == "canonical" else "legacy"
        record_input["working_memory"] = {} if core else build_working_memory(
            state.get("last_valid_review_summary") or state.get("review_summary"), record_input,
            state["unit"].id, state.get("latest_review_attempt"))[0]
        language = build_language_context((item.file_path for item in state["unit_files"]),
            state["parent_state"].get("file_index") or [], state["parent_state"].get("project_meta") or {})
        record_input["review_guidance"] = render_language_rule_context(language)
        if core:
            record_input["input_degradation"] = {"reason": "diagnosis_background_budget_degraded",
                "omitted": ["unit_plan", "working_memory"], "evidence_coverage": "complete"}
        return (pr, state["unit_files"], self._enhanced_diff(state["unit_diff"], state["context"], plan, language),
                state["parent_state"].get("model"), record_input)

    def _diagnosis_estimate(self, args):
        estimator = getattr(self.provider, "unit_diagnosis_admission", None)
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

            if summary.last_valid_snapshot == input_snapshot(self._diagnosis_args(state)[-1]):
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
            observer.update({key: state[key] for key in ("review_summary", "context", "unit_plan", "plan_status", "model_usages", "budget", "input_coverage") if key in state})

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
        return {
            "next_action": None,
            "pending_issues": [],
            "issues": [*state["issues"], *accepted],
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
        if action.action == AgentActionName.request_human:
            return {
                "done": False,
                "needs_human": True,
                "error": "review unit requires human input",
                "human_request": action.human_request,
                "terminal_reason": ReviewUnitTerminalReason.human_required,
            }
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
    def _budget_for(unit: ReviewUnit) -> ExecutionBudget:
        if unit.complexity == ReviewUnitComplexity.small:
            return ExecutionBudget(
                max_context_retrievals=4,
                max_diagnosis_attempts=1,
                max_patch_attempts=0,
                max_model_calls=3,
                max_token_usage=max(12_000, unit.estimated_tokens + 8_192),
            )
        if unit.complexity == ReviewUnitComplexity.medium:
            return ExecutionBudget(
                max_context_retrievals=8,
                max_diagnosis_attempts=2,
                max_patch_attempts=0,
                max_model_calls=5,
                max_token_usage=max(24_000, unit.estimated_tokens + 12_000),
            )
        return ExecutionBudget(
            max_context_retrievals=12,
            max_diagnosis_attempts=3,
            max_patch_attempts=0,
            max_model_calls=7,
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
    ) -> dict[str, Any]:
        readable = scope.readable_files
        language_context = build_language_context(
            unit.primary_files,
            state.get("file_index") or [],
            state.get("project_meta") or {},
        )

        record_input = build_record_input(changed_files, context, unit_plan,
            str(state.get("head_sha") or ""), str(state.get("base_sha") or ""))
        record_input["pr_intent"] = build_pr_intent(state.get("pr_info"))
        memory, restored = build_working_memory(review_summary, record_input, unit.id, latest_attempt)
        restored_ids = {item["id"] for item in restored}
        return {
            "task_id": state.get("task_id"),
            "review_unit_id": unit.id,
            "review_unit": unit.model_dump(mode="json"),
            "review_tool_scope": scope.model_dump(mode="json"),
            "unit_diff": unit_diff,
            "unit_plan": unit_plan.model_dump(mode="json") if unit_plan else None,
            "pr_intent": record_input["pr_intent"],
            "working_memory": memory,
            "memory_evidence": restored,
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
            issue = issue.model_copy(update={"review_unit_id": unit.id})
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
            accepted.append(issue.model_copy(update={"review_unit_id": unit.id}))
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
