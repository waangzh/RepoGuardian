"""确定性计划节点与 Review Unit 并发聚合节点。"""

from typing import Any

from app.agents.providers import build_provider
from app.core.config import settings
from app.graph.nodes._events import append_step
from app.graph.state import ReviewState
from app.models.review import ReviewPlan, ReviewUnitResult, ReviewUnitStatus
from app.services.review_planner import DeterministicReviewPlanner
from app.services.review_unit_executor import ReviewUnitExecutor


async def review_plan_node(state: ReviewState) -> ReviewState:
    planner: Any = state.get("_review_planner") or DeterministicReviewPlanner()
    plan = planner.plan(
        state.get("changed_files") or [],
        base_sha=state.get("base_sha") or "",
        head_sha=state.get("head_sha") or "",
        file_index=state.get("file_index") or [],
        symbol_index=state.get("symbol_index") or [],
    )
    return ReviewState(
        review_plan=plan.model_dump(mode="json"),
        review_units=[unit.model_dump(mode="json") for unit in plan.review_units],
        excluded_files=[item.model_dump(mode="json") for item in plan.excluded_files],
        warnings=list(state.get("warnings") or []) + plan.warnings,
        step_progress=append_step(
            state,
            "review_plan",
            "completed",
            f"生成 {len(plan.review_units)} 个 Review Unit，排除 {len(plan.excluded_files)} 个文件",
        ),
    )


async def review_units_node(state: ReviewState) -> ReviewState:
    plan = ReviewPlan.model_validate(state.get("review_plan") or {
        "planner_version": "unknown",
        "review_units": state.get("review_units") or [],
    })
    if not plan.review_units:
        return ReviewState(
            status="reviewing",
            review_unit_results=[],
            review_issues=[],
            step_progress=append_step(state, "review_units", "completed", "没有可审查的 Unit"),
        )

    executor: Any = state.get("_review_unit_executor")
    if executor is None:
        provider = state.get("_provider") or build_provider(
            settings.repoguardian_provider,
            settings.openai_api_key,
            settings.openai_base_url,
            settings.repoguardian_model,
        )
        executor = ReviewUnitExecutor(
            provider,
            concurrency=settings.repoguardian_review_unit_concurrency,
            timeout_seconds=settings.repoguardian_review_unit_timeout_seconds,
        )
    results: list[ReviewUnitResult] = await executor.execute(plan.review_units, dict(state))
    successful = [item for item in results if item.status == ReviewUnitStatus.completed]
    failed = [item for item in results if item.status != ReviewUnitStatus.completed]
    if failed and not successful:
        details = "; ".join(
            f"{item.review_unit_id}: {item.error or item.status.value}" for item in failed
        )
        return ReviewState(
            status="failed",
            error=f"all review units failed: {details}",
            review_unit_results=[item.model_dump(mode="json") for item in results],
            review_issues=[],
            context_snippets=[],
            agent_events=[
                *(state.get("agent_events") or []),
                *(event.model_dump(mode="json") for item in results for event in item.messages),
            ],
            step_progress=append_step(
                state, "review_units", "failed", "全部 Review Unit 执行失败"
            ),
        )

    issues = [issue for item in successful for issue in item.issues]
    snippets = [snippet for item in successful for snippet in item.context_snippets]
    events = [event for item in results for event in item.messages]
    warnings = list(state.get("warnings") or [])
    if failed:
        warnings.append(
            f"{len(failed)} 个 Review Unit 失败，其他 {len(successful)} 个 Unit 已完成"
        )
    return ReviewState(
        status="reviewing",
        review_unit_results=[item.model_dump(mode="json") for item in results],
        review_issues=[item.model_dump(mode="json") for item in issues],
        context_snippets=[item.model_dump(mode="json") for item in snippets],
        agent_events=[
            *(state.get("agent_events") or []),
            *(item.model_dump(mode="json") for item in events),
        ],
        warnings=warnings,
        step_progress=append_step(
            state,
            "review_units",
            "completed",
            f"完成 {len(successful)}/{len(results)} 个 Review Unit",
        ),
    )
