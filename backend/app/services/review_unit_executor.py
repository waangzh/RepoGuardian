"""Review Unit 独立执行与有界并发调度。"""

from __future__ import annotations

import asyncio
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
    ContextSnippet,
    ExecutionBudget,
    HumanReviewRequest,
    PullRequestInfo,
    ReviewIssue,
    ReviewPhase,
    ReviewToolScope,
    ReviewUnit,
    ReviewUnitComplexity,
    ReviewUnitResult,
    ReviewUnitStatus,
    ReviewUnitToolEvent,
)
from app.services.review_planner import DeterministicReviewPlanner
from app.tools.code_search import CodeSearchTool
from app.graph.checkpointer import unit_thread_config
from app.graph.policies import UNIT_ACTION_ROUTES, UNIT_ALLOWED_ACTIONS


class _ReviewUnitGraphState(TypedDict, total=False):
    """单个 Review Unit 子图的隔离状态。"""

    parent_state: dict[str, Any]
    unit: ReviewUnit
    scope: ReviewToolScope
    unit_files: list[ChangedFile]
    unit_diff: str
    budget: ExecutionBudget
    skip_plan: bool
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
    pending_consumed: bool
    done: bool
    error: str | None
    needs_human: bool
    human_request: HumanReviewRequest | None


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
        all_changed = [ChangedFile.model_validate(item) for item in state.get("changed_files") or []]
        by_path = {item.file_path: item for item in all_changed}
        unit_files = self._unit_changed_files(unit, by_path)
        scope = self.planner.build_scope(unit, state.get("repo_path") or None)
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
        }
        config = None
        if getattr(self.unit_graph, "checkpointer", None) is not None:
            config = unit_thread_config(str(state.get("task_id") or "unknown"), unit.id)
        result = await self.unit_graph.ainvoke(graph_state, config=config)
        return ReviewUnitResult(
            review_unit_id=unit.id,
            status=(
                ReviewUnitStatus.needs_human
                if result.get("needs_human")
                else ReviewUnitStatus.completed
                if result.get("done") and not result.get("error")
                else ReviewUnitStatus.failed
            ),
            plan_skipped=skip_plan,
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
        action, budget, legacy, model_usages = await self._decide_unit(state)
        return {
            "next_action": action,
            "budget": budget,
            "legacy_review_action": state.get("legacy_review_action", False) or legacy,
            "model_usages": model_usages,
            "messages": [*state["messages"], self._event(
                state["unit"].id, action, "selected", action.reason
            )],
        }

    async def _agent_decide_node(
        self, state: "_ReviewUnitGraphState"
    ) -> "_ReviewUnitGraphState":
        pending = state.get("next_action")
        if pending is not None:
            return {"next_action": pending, "pending_consumed": True}
        if state["issue_round_completed"] and state.get("legacy_review_action", False):
            return {"next_action": AgentAction(
                action=AgentActionName.task_done,
                reason="兼容旧 Provider：完成一次结构化问题报告后显式结束 Unit",
            )}
        action, budget, legacy, model_usages = await self._decide_unit(state)
        return {
            "next_action": action,
            "budget": budget,
            "legacy_review_action": state.get("legacy_review_action", False) or legacy,
            "model_usages": model_usages,
            "messages": [*state["messages"], self._event(
                state["unit"].id, action, "selected", action.reason
            )],
        }

    async def _decide_unit(
        self, state: "_ReviewUnitGraphState"
    ) -> tuple[AgentAction, ExecutionBudget, bool, list[dict[str, Any]]]:
        budget = state["budget"]
        if not budget.can_consume(model_calls=1, token_usage=600):
            return (
                AgentAction(action="task_done", reason="Unit 模型调用预算已耗尽"),
                budget,
                False,
                list(state.get("model_usages") or []),
            )
        budget = budget.consume(model_calls=1, token_usage=600)
        decision_state = self._unit_state(
            state["parent_state"], state["unit"], state["scope"],
            state["unit_files"], budget, state["context"],
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
        return action, budget, legacy_review, model_usages

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
        if not budget.can_consume(context_retrievals=1):
            events.append(ReviewUnitToolEvent(
                review_unit_id=state["unit"].id,
                tool="code_search",
                status="rejected",
                detail="context retrieval budget exhausted",
            ))
            return {"next_action": None, "tool_events": events}
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
        budget = budget.consume(context_retrievals=1)
        try:
            snippets = await CodeSearchTool().retrieve_context(
                changed_files=[item.model_dump(mode="json") for item in state["unit_files"]],
                symbol_index=state["parent_state"].get("symbol_index") or [],
                file_index=state["parent_state"].get("file_index") or [],
                repo_path=state["parent_state"].get("repo_path", ""),
                plan=plan,
                scope=state["scope"],
            )
            existing = {
                (item.get("file"), item.get("start_line"), item.get("end_line"))
                for item in state["context"]
            }
            new_items = [
                item for item in snippets
                if (item.get("file"), item.get("start_line"), item.get("end_line")) not in existing
            ]
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
            return {"pending_issues": [], "next_action": None}
        budget = budget.consume(diagnosis_attempts=1, model_calls=1, token_usage=4_096)
        pr = PullRequestInfo.model_validate(state["parent_state"].get("pr_info") or {})
        from app.services.model_usage import annotate_usage, append_usage, unpack_model_call

        raw_result = await self.provider.review(
            pr,
            state["unit_files"],
            self._enhanced_diff(state["unit_diff"], state["context"]),
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
            }
        return {
            "done": True,
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
                max_context_retrievals=0,
                max_diagnosis_attempts=1,
                max_patch_attempts=0,
                max_model_calls=3,
                max_token_usage=max(6_000, unit.estimated_tokens + 4_096),
            )
        if unit.complexity == ReviewUnitComplexity.medium:
            return ExecutionBudget(
                max_context_retrievals=1,
                max_diagnosis_attempts=1,
                max_patch_attempts=0,
                max_model_calls=4,
                max_token_usage=max(12_000, unit.estimated_tokens + 6_000),
            )
        return ExecutionBudget(
            max_context_retrievals=2,
            max_diagnosis_attempts=1,
            max_patch_attempts=0,
                max_model_calls=6,
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
    ) -> dict[str, Any]:
        readable = scope.readable_files
        return {
            "task_id": state.get("task_id"),
            "review_unit_id": unit.id,
            "review_unit": unit.model_dump(mode="json"),
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
    def _enhanced_diff(unit_diff: str, context: list[dict[str, Any]]) -> str:
        if not context:
            return unit_diff
        sections = ["## Unit scoped context"]
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
            sections.append(snippet.get("content", ""))
        sections.extend(["## Unit diff", unit_diff])
        return "\n".join(sections)

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
