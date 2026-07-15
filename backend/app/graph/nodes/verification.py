"""在进入修复子图前固化 Head 基线结论。"""

from app.graph.nodes._events import append_step
from app.graph.state import ReviewState
from app.models.review import (
    FailureKind,
    ProjectProfile,
    ReviewPhase,
    ValidationDelta,
    ValidationSnapshot,
    ValidationStage,
)
from app.projects.registry import default_project_registry
from app.services.validation_service import (
    ValidationService,
    blocks_auto_repair,
    compare_snapshots,
)
from app.tools.command_runner import LocalCommandExecutor


async def verification_node(state: ReviewState) -> ReviewState:
    snapshots = [ValidationSnapshot.model_validate(item) for item in state.get("validation_snapshots") or []]
    deltas = [ValidationDelta.model_validate(item) for item in state.get("validation_deltas") or []]
    blocked = any(blocks_auto_repair(snapshot) for snapshot in snapshots)
    if deltas and deltas[-1].introduced_failure:
        message = "Head 相对 Base 引入代码回归"
    elif blocked:
        message = "验证发现环境或基础设施失败，已禁止自动修复"
    else:
        message = "Base 与 Head 验证结论已固化"
    return ReviewState(
        phase=ReviewPhase.verification,
        validation_blocked=blocked,
        step_progress=append_step(state, "verification", "completed", message),
    )


async def patched_validation_node(state: ReviewState) -> ReviewState:
    """在临时 Head 工作树应用补丁后执行 Patched 阶段的同一验证集。"""
    snapshots = [ValidationSnapshot.model_validate(item) for item in state.get("validation_snapshots") or []]
    head_snapshot = next((item for item in snapshots if item.stage == ValidationStage.head), None)
    profile_data = state.get("project_profile")
    if head_snapshot is None or not profile_data:
        return ReviewState(
            phase=ReviewPhase.validation,
            validation_blocked=True,
            step_progress=append_step(state, "patched_validation", "failed", "缺少 Head 基线，无法验证补丁"),
        )

    profile = ProjectProfile.model_validate(profile_data)
    registry = state.get("_project_registry") or default_project_registry
    adapter = registry.get(profile.adapter_id)
    if adapter is None:
        patched = ValidationSnapshot(
            stage=ValidationStage.patched,
            sha=head_snapshot.sha,
            passed=False,
            failure_kind=FailureKind.unknown,
            failure_detail="project adapter is unavailable",
        )
    else:
        executor = state.get("_command_executor") or LocalCommandExecutor()
        patched = await ValidationService(adapter, executor).run_stage(
            state.get("repo_path", ""), profile, ValidationStage.patched, head_snapshot.sha
        )

    delta = compare_snapshots(head_snapshot, patched)
    updated_snapshots = snapshots + [patched]
    updated_deltas = [
        ValidationDelta.model_validate(item) for item in state.get("validation_deltas") or []
    ] + [delta]
    blocked = any(blocks_auto_repair(snapshot) for snapshot in updated_snapshots)
    message = (
        "补丁验证通过" if patched.passed else f"补丁验证失败：{patched.failure_kind.value}"
    )
    return ReviewState(
        phase=ReviewPhase.validation,
        validation_snapshots=[item.model_dump(mode="json") for item in updated_snapshots],
        validation_deltas=[item.model_dump(mode="json") for item in updated_deltas],
        validation_blocked=blocked,
        test_results=[item.model_dump(mode="json") for item in patched.command_results],
        step_progress=append_step(state, "patched_validation", "completed", message),
    )
