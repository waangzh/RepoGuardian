"""风险筛查后的一轮有界协调节点。"""

from app.agents.providers import build_provider
from app.core.config import settings
from app.graph.nodes._events import append_step
from app.graph.state import ReviewState, ReviewRunContext
from app.services.cross_unit_coordination import CrossUnitCoordinationService
from app.services.review_repository import ReviewRepository
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.runtime import Runtime


async def cross_unit_coordination_node(state: ReviewState, config: RunnableConfig = None,
                                      runtime: Runtime[ReviewRunContext] = None) -> ReviewState:
    if (state.get("cross_unit_risk") or {}).get("decision") == "skip":
        return ReviewState()
    provider = state.get("_provider") or build_provider(
        settings.repoguardian_provider, settings.openai_api_key,
        settings.openai_base_url, settings.repoguardian_model,
    )
    repository = state.get("_coordination_repository")
    if repository is None and runtime is not None:
        repository = (runtime.context or {}).get("coordination_repository")
    if repository is None and state.get("_human_interrupt_enabled"):
        repository = ReviewRepository()
    try:
        writer = get_stream_writer()
    except RuntimeError:
        writer = None
    result = await CrossUnitCoordinationService(provider, repository=repository,
        lease=(config or {}).get("configurable", {}).get("review_job_lease"),
        progress=(lambda delta: writer({"kind": "coordination_progress", "state": delta})) if writer else None,
    ).run(dict(state))
    plan = result.get("coordination_plan") or state.get("coordination_plan") or {}
    status = plan.get("status", "unresolved")
    return ReviewState(**result, step_progress=append_step(
        state, "cross_unit_coordination", "completed" if status == "completed" else "failed",
        "跨 Unit 协调：" + status,
    ))
