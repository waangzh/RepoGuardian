from app.graph.nodes._events import append_event, append_step
from app.graph.state import ReviewState
from app.models.review import AgentAction


async def human_required_node(state: ReviewState) -> ReviewState:
    action = AgentAction.model_validate(state.get("next_action") or {
        "action": "request_human",
        "reason": "Human approval is required.",
    })
    message = "Agent requested human review before continuing."
    return ReviewState(
        agent_events=append_event(state, action.action, action.reason, "completed", message),
        step_progress=append_step(state, "human_required", "completed", message),
    )
