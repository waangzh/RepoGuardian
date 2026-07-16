"""不可跳过的 Base / PR Head 基线验证节点。"""

import asyncio
from typing import Any

from app.graph.nodes._events import append_step
from app.graph.state import ReviewState
from app.models.review import (
    FailureKind,
    ProjectProfile,
    TestRunResult,
    ValidationSnapshot,
    ValidationStage,
)
from app.projects.registry import default_project_registry
from app.services.validation_service import ValidationService, blocks_auto_repair, compare_snapshots
from app.tools.command_runner import build_command_executor


async def baseline_node(state: ReviewState) -> ReviewState:
    """依次验证 Base SHA 与 PR Head SHA，并最终恢复到 Head 工作树。"""
    base_sha = state.get("base_sha") or "unknown-base"
    head_sha = state.get("head_sha") or "unknown-head"
    profile_data = state.get("project_profile")
    if not profile_data:
        snapshots = [_unsupported_snapshot(ValidationStage.base, base_sha), _unsupported_snapshot(ValidationStage.head, head_sha)]
        return _baseline_state(state, snapshots, validation_ready=False)

    profile = ProjectProfile.model_validate(profile_data)
    registry = state.get("_project_registry") or default_project_registry
    adapter = registry.get(profile.adapter_id)
    if adapter is None:
        snapshots = [_unsupported_snapshot(ValidationStage.base, base_sha), _unsupported_snapshot(ValidationStage.head, head_sha)]
        return _baseline_state(state, snapshots, validation_ready=False)

    executor = state.get("_command_executor") or build_command_executor()
    validator = ValidationService(adapter, executor)
    git_tool: Any = state.get("_git_tool")
    repo_path = state.get("repo_path", "")

    base_snapshot = await _checkout_and_validate(
        git_tool, repo_path, base_sha, ValidationStage.base, profile, validator
    )
    head_snapshot = await _checkout_and_validate(
        git_tool, repo_path, head_sha, ValidationStage.head, profile, validator
    )
    return _baseline_state(state, [base_snapshot, head_snapshot], validation_ready=head_snapshot.failure_kind != FailureKind.infrastructure)


async def _checkout_and_validate(
    git_tool: Any,
    repo_path: str,
    sha: str,
    stage: ValidationStage,
    profile: ProjectProfile,
    validator: ValidationService,
) -> ValidationSnapshot:
    try:
        await asyncio.to_thread(git_tool.checkout_sha, repo_path, sha)
    except Exception as exc:
        return ValidationSnapshot(
            stage=stage,
            sha=sha,
            command_results=[
                TestRunResult(
                    tool="validation",
                    command="git.checkout",
                    exit_code=125,
                    stderr=f"checkout failed: {type(exc).__name__}: {exc}",
                    passed=False,
                )
            ],
            passed=False,
            failure_kind=FailureKind.infrastructure,
            failure_detail="cannot checkout validation SHA",
        )
    return await validator.run_stage(repo_path, profile, stage, sha)


def _unsupported_snapshot(stage: ValidationStage, sha: str) -> ValidationSnapshot:
    return ValidationSnapshot(
        stage=stage,
        sha=sha,
        command_results=[],
        passed=False,
        failure_kind=FailureKind.unknown,
        failure_detail="no supported project adapter detected",
    )


def _baseline_state(
    state: ReviewState,
    snapshots: list[ValidationSnapshot],
    *,
    validation_ready: bool,
) -> ReviewState:
    delta = compare_snapshots(snapshots[0], snapshots[1])
    blocked = any(blocks_auto_repair(snapshot) for snapshot in snapshots)
    message = (
        f"Base={'通过' if snapshots[0].passed else snapshots[0].failure_kind.value}，"
        f"Head={'通过' if snapshots[1].passed else snapshots[1].failure_kind.value}"
    )
    return ReviewState(
        phase="baseline",
        validation_snapshots=[item.model_dump(mode="json") for item in snapshots],
        validation_deltas=[delta.model_dump(mode="json")],
        validation_blocked=blocked,
        validation_ready=validation_ready,
        step_progress=append_step(state, "baseline", "completed", message),
    )
