"""阶段一问题验证占位节点。"""

from app.graph.nodes._events import append_step
from app.graph.state import ReviewState
from app.models.review import ReviewPhase


async def verification_node(state: ReviewState) -> ReviewState:
    return ReviewState(
        phase=ReviewPhase.verification,
        step_progress=append_step(state, "verification", "completed", "问题验证尚未接入"),
    )
