from typing import Any

from app.agents.providers import LLMProviderError, build_provider
from app.core.config import settings
from app.graph.nodes._events import append_event, append_step
from app.graph.state import ReviewState
from app.models.review import AgentAction


async def agent_decide_node(state: ReviewState) -> ReviewState:
    loop_count = int(state.get("agent_loop_count") or 0) + 1
    max_loops = int(state.get("max_agent_loops") or 6)
    fix_iteration = int(state.get("fix_iteration") or 0)
    max_fix_iterations = int(state.get("max_fix_iterations") or 3)

    if loop_count > max_loops:
        action = AgentAction(
            action="finish_report",
            reason=f"Agent loop limit reached ({max_loops}); finishing report.",
        )
        return _with_action(state, action, loop_count, "completed", "Agent loop limit reached")

    if fix_iteration >= max_fix_iterations and _has_failed_tests(state):
        action = AgentAction(
            action="finish_report",
            reason=f"Fix retry limit reached ({max_fix_iterations}); finishing report.",
        )
        return _with_action(state, action, loop_count, "completed", "Fix retry limit reached")

    provider: Any = state.get("_provider") or build_provider(
        settings.repoguardian_provider,
        settings.openai_api_key,
        settings.openai_base_url,
        settings.repoguardian_model,
    )

    try:
        action = await provider.decide(dict(state), state.get("model"))
        invalid_action_count = int(state.get("invalid_action_count") or 0)
    except (LLMProviderError, ValueError) as exc:
        invalid_action_count = int(state.get("invalid_action_count") or 0) + 1
        if invalid_action_count >= 2:
            action = AgentAction(
                action="finish_report",
                reason="LLM action output was invalid twice; finishing report.",
            )
        else:
            action = AgentAction(
                action="review_code",
                reason=f"LLM action output invalid; fallback to review. Error: {exc}",
            )
        result = _with_action(state, action, loop_count, "failed", str(exc))
        result["invalid_action_count"] = invalid_action_count
        return ReviewState(**result)

    result = _with_action(state, action, loop_count, "selected", action.reason)
    result["invalid_action_count"] = invalid_action_count
    return ReviewState(**result)


def _with_action(
    state: ReviewState,
    action: AgentAction,
    loop_count: int,
    status: str,
    message: str,
) -> dict[str, Any]:
    return {
        "next_action": action.model_dump(mode="json"),
        "agent_loop_count": loop_count,
        "agent_events": append_event(state, action.action, action.reason, status, message),
        "step_progress": append_step(
            state,
            f"agent:{action.action.value}",
            "completed",
            action.reason,
        ),
    }


def _has_failed_tests(state: ReviewState) -> bool:
    return any(not result.get("passed", False) for result in state.get("test_results") or [])
