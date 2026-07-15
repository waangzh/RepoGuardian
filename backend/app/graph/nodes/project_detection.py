"""项目检测阶段，复用仓库索引结果而不调用 Agent。"""

from pathlib import Path

from app.graph.nodes._events import append_step
from app.graph.state import ReviewState
from app.models.review import ReviewPhase
from app.projects.registry import default_project_registry


async def project_detection_node(state: ReviewState) -> ReviewState:
    registry = state.get("_project_registry") or default_project_registry
    profile = registry.detect(Path(state.get("repo_path", "")))
    metadata = state.get("project_meta") or {}
    language = profile.language if profile else metadata.get("language", "unknown")
    framework = metadata.get("framework") or "unknown"
    if profile:
        message = (
            f"检测到 {profile.adapter_id} 项目，标记文件："
            f"{', '.join(profile.detected_files) or 'Python 源文件'}"
        )
    else:
        message = "未检测到受支持的项目适配器"
    return ReviewState(
        phase=ReviewPhase.project_detection,
        project_adapter_id=profile.adapter_id if profile else None,
        project_profile=profile.model_dump(mode="json") if profile else None,
        step_progress=append_step(
            state,
            "project_detection",
            "completed",
            f"{message}；语言 {language}，框架 {framework}",
        ),
    )
