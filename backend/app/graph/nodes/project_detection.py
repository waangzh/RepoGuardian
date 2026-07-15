"""项目检测阶段，复用仓库索引结果而不调用 Agent。"""

from app.graph.nodes._events import append_step
from app.graph.state import ReviewState
from app.models.review import ReviewPhase


async def project_detection_node(state: ReviewState) -> ReviewState:
    metadata = state.get("project_meta") or {}
    language = metadata.get("language", "unknown")
    framework = metadata.get("framework") or "unknown"
    return ReviewState(
        phase=ReviewPhase.project_detection,
        step_progress=append_step(
            state,
            "project_detection",
            "completed",
            f"检测到语言 {language}，框架 {framework}",
        ),
    )
