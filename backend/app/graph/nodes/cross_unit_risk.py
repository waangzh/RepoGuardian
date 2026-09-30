"""跨 Unit 风险筛查节点；不改变候选问题和原始覆盖。"""

from app.graph.nodes._events import append_step
from app.graph.state import ReviewState
from app.services.cross_unit_risk import CrossUnitRiskService


async def cross_unit_risk_node(state: ReviewState) -> ReviewState:
    assessment = CrossUnitRiskService().assess(dict(state))
    return ReviewState(
        cross_unit_risk=assessment.model_dump(mode="json"),
        step_progress=append_step(state, "cross_unit_risk", "completed",
                                  f"跨 Unit 风险筛查：{assessment.decision}"),
    )
