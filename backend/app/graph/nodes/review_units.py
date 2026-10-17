"""确定性 Review Unit 计划节点。"""

from typing import Any

from app.core.config import settings
from app.graph.nodes._events import append_step
from app.graph.state import ReviewState
from app.review.diff_base import review_diff_base
from app.services.review_planner import DeterministicReviewPlanner
from app.services.review_input_context import build_pr_intent


async def review_plan_node(state: ReviewState) -> ReviewState:
    planner: Any = state.get("_review_planner") or DeterministicReviewPlanner()
    plan = planner.plan(
        state.get("changed_files") or [],
        base_sha=review_diff_base(state),
        head_sha=state.get("head_sha") or "",
        file_index=state.get("file_index") or [],
        symbol_index=state.get("symbol_index") or [],
        repository_graph=state.get("repository_graph") or {},
        model=state.get("model") or settings.repoguardian_model,
        provider=(state.get("task_runtime_configuration") or {}).get("provider", settings.repoguardian_provider),
        pr_intent_hash=build_pr_intent(state.get("pr_info"))["intent_hash"],
    )
    configuration = state.get("task_runtime_configuration") or {}
    if configuration.get("effective_plan"):
        from app.models.review_plan import ReviewPlanOptions
        from app.services.review_plan import apply_scope
        from app.services.fingerprints import stable_hash
        options = ReviewPlanOptions.model_validate(configuration["effective_plan"])
        plan = apply_scope(plan, options)
        if configuration.get("initial_plan_hash") != stable_hash(plan.model_dump(mode="json")):
            raise ValueError("review_plan_stale: deterministic plan changed")
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
