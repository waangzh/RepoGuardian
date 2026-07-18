"""Review Unit 独立执行与有界并发调度。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from app.agents.providers import LLMProvider
from app.models.review import (
    AgentAction,
    AgentActionName,
    AgentEvent,
    ChangedFile,
    ContextRetrievalPlan,
    ContextSnippet,
    ExecutionBudget,
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


class ReviewUnitExecutor:
    """使用固定数量 worker 执行 Unit，不按 Unit 数量无限创建任务。"""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        concurrency: int,
        timeout_seconds: int,
        planner: DeterministicReviewPlanner | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("review unit concurrency must be positive")
        if timeout_seconds < 1:
            raise ValueError("review unit timeout must be positive")
        self.provider = provider
        self.concurrency = concurrency
        self.timeout_seconds = timeout_seconds
        self.planner = planner or DeterministicReviewPlanner()

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
            return ReviewUnitResult(
                review_unit_id=unit.id,
                status=ReviewUnitStatus.failed,
                plan_skipped=False,
                execution_budget=self._budget_for(unit),
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
        scope = self.planner.build_scope(unit)
        budget = self._budget_for(unit)
        skip_plan = self.planner.should_skip_plan(unit, all_changed)
        messages: list[AgentEvent] = []
        tool_events: list[ReviewUnitToolEvent] = []
        context: list[dict[str, Any]] = []

        if not skip_plan:
            budget = budget.consume(model_calls=1, token_usage=1_200)
            action = await self.provider.decide(
                self._unit_state(state, unit, scope, unit_files, budget, context),
                state.get("model"),
            )
            messages.append(self._event(unit.id, action, "selected", action.reason))
            if action.action == AgentActionName.retrieve_context:
                plan = ContextRetrievalPlan.model_validate(action.tool_args["plan"])
                try:
                    context = await CodeSearchTool().retrieve_context(
                        changed_files=[item.model_dump(mode="json") for item in unit_files],
                        symbol_index=state.get("symbol_index") or [],
                        file_index=state.get("file_index") or [],
                        repo_path=state.get("repo_path", ""),
                        plan=plan,
                        scope=scope,
                    )
                    budget = budget.consume(context_retrievals=1)
                    tool_events.append(ReviewUnitToolEvent(
                        review_unit_id=unit.id,
                        tool="code_search",
                        status="completed",
                        result_count=len(context),
                    ))
                except ValueError as exc:
                    tool_events.append(ReviewUnitToolEvent(
                        review_unit_id=unit.id,
                        tool="code_search",
                        status="rejected",
                        detail=str(exc),
                    ))
            elif action.action == AgentActionName.request_human:
                return ReviewUnitResult(
                    review_unit_id=unit.id,
                    status=ReviewUnitStatus.failed,
                    plan_skipped=False,
                    messages=messages,
                    tool_events=tool_events,
                    execution_budget=budget,
                    error="review unit requires human input",
                )

        budget = budget.consume(diagnosis_attempts=1, model_calls=1, token_usage=4_096)
        pr = PullRequestInfo.model_validate(state.get("pr_info") or {})
        unit_diff = self._unit_diff(unit, by_path)
        enhanced_diff = self._enhanced_diff(unit_diff, context)
        model_issues = await self.provider.review(pr, unit_files, enhanced_diff, state.get("model"))
        issues = self._filter_issues(model_issues, unit, scope)
        messages.append(AgentEvent(
            action=AgentActionName.review_code,
            reason="执行 Unit 独立审查",
            status="completed",
            message=f"发现 {len(issues)} 个问题",
            review_unit_id=unit.id,
        ))
        snippets = [ContextSnippet.model_validate(item) for item in context]
        return ReviewUnitResult(
            review_unit_id=unit.id,
            status=ReviewUnitStatus.completed,
            plan_skipped=skip_plan,
            issues=issues,
            context_snippets=snippets,
            messages=messages,
            tool_events=tool_events,
            execution_budget=budget,
        )

    @staticmethod
    def _budget_for(unit: ReviewUnit) -> ExecutionBudget:
        if unit.complexity == ReviewUnitComplexity.small:
            return ExecutionBudget(
                max_context_retrievals=0,
                max_diagnosis_attempts=1,
                max_patch_attempts=0,
                max_model_calls=1,
                max_token_usage=max(6_000, unit.estimated_tokens + 4_096),
            )
        if unit.complexity == ReviewUnitComplexity.medium:
            return ExecutionBudget(
                max_context_retrievals=1,
                max_diagnosis_attempts=1,
                max_patch_attempts=0,
                max_model_calls=2,
                max_token_usage=max(12_000, unit.estimated_tokens + 6_000),
            )
        return ExecutionBudget(
            max_context_retrievals=2,
            max_diagnosis_attempts=1,
            max_patch_attempts=0,
            max_model_calls=3,
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
            sections.append(
                f"### {snippet.get('file')}:{snippet.get('start_line')}-{snippet.get('end_line')}"
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
            if issue.id in seen or issue.file_path not in scope.commentable_files:
                continue
            if issue.line_no is None or issue.line_no < 1:
                continue
            if any(
                location.file_path not in scope.readable_files
                for location in issue.evidence_locations
            ):
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
