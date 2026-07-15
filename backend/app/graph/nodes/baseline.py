"""阶段一基线占位节点。"""

from app.graph.nodes._events import append_step
from app.graph.state import ReviewState
from app.models.review import ReviewPhase


async def baseline_node(state: ReviewState) -> ReviewState:
    return ReviewState(
        phase=ReviewPhase.baseline,
        step_progress=append_step(state, "baseline", "completed", "基线检查尚未接入"),
    )
