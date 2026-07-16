"""在进入修复子图前固化 Head 基线结论。"""

from app.graph.nodes._events import append_step
from app.graph.nodes.patch import restore_patch_workspace
from app.graph.state import ReviewState
from app.models.review import (
    FailureKind,
    PatchResult,
    PatchStatus,
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
from app.tools.command_runner import build_command_executor


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
    active_patch_id = state.get("active_patch_id")
    if head_snapshot is None or not profile_data:
        return ReviewState(
            phase=ReviewPhase.validation,
            validation_blocked=True,
            active_patch_validation_passed=False,
            step_progress=append_step(state, "patched_validation", "failed", "缺少 Head 基线，无法验证补丁"),
        )

    profile = ProjectProfile.model_validate(profile_data)
    registry = state.get("_project_registry") or default_project_registry
    adapter = registry.get(profile.adapter_id)
    patched: ValidationSnapshot
    try:
        if adapter is None:
            patched = ValidationSnapshot(
                stage=ValidationStage.patched,
                sha=head_snapshot.sha,
                patch_id=active_patch_id,
                passed=False,
                failure_kind=FailureKind.unknown,
                failure_detail="project adapter is unavailable",
            )
        else:
            executor = state.get("_command_executor") or build_command_executor()
            patched = await ValidationService(adapter, executor).run_stage(
                state.get("repo_path", ""), profile, ValidationStage.patched, head_snapshot.sha
            )
            patched.patch_id = active_patch_id
    except Exception as exc:
        patched = ValidationSnapshot(
            stage=ValidationStage.patched,
            sha=head_snapshot.sha,
            patch_id=active_patch_id,
            passed=False,
            failure_kind=FailureKind.infrastructure,
            failure_detail=f"patched validation failed: {type(exc).__name__}: {exc}",
        )
    finally:
        cleanup_error = await restore_patch_workspace(state)

    if cleanup_error:
        patched.passed = False
        patched.failure_kind = FailureKind.infrastructure
        patched.failure_detail = (
            f"{patched.failure_detail or 'patched validation completed'}; cleanup failed: {cleanup_error}"
        )

    delta = compare_snapshots(head_snapshot, patched)
    updated_snapshots = snapshots + [patched]
    updated_deltas = [
        ValidationDelta.model_validate(item) for item in state.get("validation_deltas") or []
    ] + [delta]
    blocked = any(blocks_auto_repair(snapshot) for snapshot in updated_snapshots)
    updated_patches = [PatchResult.model_validate(item) for item in state.get("patches") or []]
    for patch in updated_patches:
        if patch.id == active_patch_id:
            patch.status = (
                PatchStatus.validation_passed if patched.passed else PatchStatus.validation_failed
            )
            patch.validation_snapshot_id = patched.id
            break
    message = (
        "补丁验证通过" if patched.passed else f"补丁验证失败：{patched.failure_kind.value}"
    )
    return ReviewState(
        phase=ReviewPhase.validation,
        validation_snapshots=[item.model_dump(mode="json") for item in updated_snapshots],
        validation_deltas=[item.model_dump(mode="json") for item in updated_deltas],
        validation_blocked=blocked,
        active_patch_validation_passed=patched.passed,
        patch_workspace_clean=cleanup_error is None,
        patches=[patch.model_dump(mode="json") for patch in updated_patches],
        test_results=[item.model_dump(mode="json") for item in patched.command_results],
        step_progress=append_step(state, "patched_validation", "completed", message),
    )
