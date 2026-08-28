"""Review Unit 独立执行与有界并发调度。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.providers import LLMProvider
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
)
from app.services.review_planner import DeterministicReviewPlanner
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
    ) -> None:
        if concurrency < 1:
            raise ValueError("review unit concurrency must be positive")
        if timeout_seconds < 1:
            raise ValueError("review unit timeout must be positive")
        self.provider = provider
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
        try:
            async with asyncio.timeout(self.timeout_seconds):
                return await self._execute_unit(unit, state)
        except TimeoutError:
            return ReviewUnitResult(
                review_unit_id=unit.id,
                status=ReviewUnitStatus.timed_out,
                terminal_reason=ReviewUnitTerminalReason.timed_out,
                plan_skipped=False,
                execution_budget=self._budget_for(unit),
                error=f"review unit timed out after {self.timeout_seconds} seconds",
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
                status=ReviewUnitStatus.failed,
                terminal_reason=(
                    ReviewUnitTerminalReason.provider_error
                    if isinstance(exc, LLMProviderError)
                    else ReviewUnitTerminalReason.execution_error
                ),
                plan_skipped=False,
                execution_budget=self._budget_for(unit),
                model_usages=[usage] if usage is not None else [],
                error=f"{type(exc).__name__}: {exc}",
            )

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
        graph_state: _ReviewUnitGraphState = {
            "parent_state": {key: value for key, value in state.items() if not key.startswith("_")},
            "unit": unit,
            "scope": scope,
            "unit_files": unit_files,
            "unit_diff": self._unit_diff(unit, by_path),
            "budget": budget,
            "skip_plan": skip_plan,
            "unit_plan": None,
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
        }
        config = None
        if getattr(self.unit_graph, "checkpointer", None) is not None:
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
            terminal_reason = ReviewUnitTerminalReason.execution_error
        return ReviewUnitResult(
            review_unit_id=unit.id,
            status=(
                ReviewUnitStatus.needs_human
                if result.get("needs_human")
                else ReviewUnitStatus.completed
                if result.get("done") and not result.get("error")
                else ReviewUnitStatus.failed
            ),
            terminal_reason=terminal_reason,
            plan_skipped=skip_plan,
            plan=result.get("unit_plan"),
            plan_status=result.get("plan_status"),
            plan_skip_reason=result.get("plan_skip_reason"),
            plan_error=result.get("plan_error"),
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
        if not budget.can_consume(model_calls=1, token_usage=1_200):
            return {
                "plan_status": UnitPlanStatus.skipped,
                "plan_skip_reason": "budget_insufficient",
            }
        budget = budget.consume(model_calls=1, token_usage=1_200)
        planning_state = self._unit_state(
            state["parent_state"], state["unit"], state["scope"],
            state["unit_files"], budget, state["context"],
            unit_diff=state["unit_diff"],
        )
        usage = None
        try:
            raw_result = await self.provider.plan_review_unit(
                planning_state, state["parent_state"].get("model")
            )
            plan, usage = unpack_model_call(raw_result)
            self._validate_unit_plan_scope(
                plan, state["scope"], planning_state["symbol_index"]
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, LLMProviderError):
                usage = exc.usage
            usage = annotate_usage(
                usage,
                accounted_tokens_estimate=1_200,
                review_unit_id=state["unit"].id,
                unit_complexity=state["unit"].complexity,
            )
            detail = f"{type(exc).__name__}: {exc}"
            return {
                "budget": budget,
                "plan_status": UnitPlanStatus.failed,
                "plan_skip_reason": "planning_failed",
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
            accounted_tokens_estimate=1_200,
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
        action, budget, legacy, model_usages, terminal_reason = await self._decide_unit(state)
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
        if not budget.can_consume(model_calls=1, token_usage=600):
            return (
                AgentAction(action="task_done", reason="Unit 模型调用预算已耗尽"),
                budget,
                False,
                list(state.get("model_usages") or []),
                ReviewUnitTerminalReason.model_budget_exhausted,
            )
        budget = budget.consume(model_calls=1, token_usage=600)
        decision_state = self._unit_state(
            state["parent_state"], state["unit"], state["scope"],
            state["unit_files"], budget, state["context"],
            unit_diff=state["unit_diff"],
            unit_plan=state.get("unit_plan"),
        )
        decision_state.update({
            "unit_agent": True,
            "reported_issue_count": len(state["issues"]),
            "issue_round_completed": state["issue_round_completed"],
            "retrieval_history": list(state["retrieval_history"]),
            "retrieval_no_new_rounds": state["retrieval_no_new_rounds"],
        })
        from app.services.model_usage import annotate_usage, append_usage, unpack_model_call

        raw_result = await self.provider.decide(
            decision_state, state["parent_state"].get("model")
        )
        action, usage = unpack_model_call(raw_result)
        usage = annotate_usage(
            usage,
            accounted_tokens_estimate=600,
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
        budget = state["budget"]
        if not budget.can_consume(diagnosis_attempts=1, model_calls=1, token_usage=4_096):
            return {
                "pending_issues": [],
                "next_action": None,
                "terminal_reason": ReviewUnitTerminalReason.diagnosis_budget_exhausted,
            }
        budget = budget.consume(diagnosis_attempts=1, model_calls=1, token_usage=4_096)
        pr = PullRequestInfo.model_validate(state["parent_state"].get("pr_info") or {})
        from app.services.model_usage import annotate_usage, append_usage, unpack_model_call

        raw_result = await self.provider.review(
            pr,
            state["unit_files"],
            self._enhanced_diff(
                state["unit_diff"],
                state["context"],
                state.get("unit_plan"),
                build_language_context(
                    (item.file_path for item in state["unit_files"]),
                    state["parent_state"].get("file_index") or [],
                    state["parent_state"].get("project_meta") or {},
                ),
            ),
            state["parent_state"].get("model"),
        )
        model_issues, usage = unpack_model_call(raw_result)
        usage = annotate_usage(
            usage,
            accounted_tokens_estimate=4_096,
            review_unit_id=state["unit"].id,
            unit_complexity=state["unit"].complexity,
        )
        return {
            "pending_issues": model_issues,
            "budget": budget,
            "model_usages": append_usage(state.get("model_usages") or [], usage),
        }

    async def _collect_issue_node(
        self, state: "_ReviewUnitGraphState"
    ) -> "_ReviewUnitGraphState":
        accepted = self._filter_issues(
            state.get("pending_issues") or [], state["unit"], state["scope"]
        )
        known = {issue.id for issue in state["issues"]}
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
                max_token_usage=max(6_000, unit.estimated_tokens + 4_096),
            )
        if unit.complexity == ReviewUnitComplexity.medium:
            return ExecutionBudget(
                max_context_retrievals=8,
                max_diagnosis_attempts=2,
                max_patch_attempts=0,
                max_model_calls=5,
                max_token_usage=max(12_000, unit.estimated_tokens + 6_000),
            )
        return ExecutionBudget(
            max_context_retrievals=12,
            max_diagnosis_attempts=3,
            max_patch_attempts=0,
            max_model_calls=7,
            max_token_usage=max(20_000, unit.estimated_tokens + 8_000),
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
    ) -> dict[str, Any]:
        readable = scope.readable_files
        language_context = build_language_context(
            unit.primary_files,
            state.get("file_index") or [],
            state.get("project_meta") or {},
        )

        return {
            "task_id": state.get("task_id"),
            "review_unit_id": unit.id,
            "review_unit": unit.model_dump(mode="json"),
            "review_tool_scope": scope.model_dump(mode="json"),
            "unit_diff": unit_diff,
            "unit_plan": unit_plan.model_dump(mode="json") if unit_plan else None,
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
            "context_snippets": context,
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
            if len(content) > remaining:
                if remaining < 32:
                    break
                item = {**item, "content": content[:remaining].rstrip() + "\n...(truncated)"}
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
        accepted: list[ReviewIssue] = []
        seen: set[str] = set()
        for issue in model_issues:
            if issue.id in seen:
                continue
            seen.add(issue.id)
            if issue.primary_evidence.file_path not in scope.commentable_files:
                continue
            if any(
                anchor.file_path not in scope.readable_files
                for anchor in issue.supporting_evidence
            ):
                continue
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
