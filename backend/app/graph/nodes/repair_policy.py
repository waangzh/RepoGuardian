"""确定性修复准入策略与修复子图适配节点。"""

from typing import Any

from app.graph.nodes._events import append_event, append_step
from app.graph.nodes.patch import patch_node, restore_patch_workspace
from app.graph.nodes.verification import patched_validation_node
from app.graph.policies import get_execution_budget
from app.graph.state import ReviewState
from app.models.review import AgentAction, AgentActionName, PatchResult, PatchStatus, ReviewPhase


async def repair_policy_node(state: ReviewState) -> ReviewState:
    """仅让标记为 auto_fixable 的问题进入补丁流程。"""
    if state.get("validation_blocked"):
        message = "验证存在环境、依赖、收集、超时或基础设施失败，禁止自动修复"
        return ReviewState(
            phase=ReviewPhase.repair,
            repair_enabled=False,
            step_progress=append_step(state, "repair_policy", "completed", message),
        )
    budget = get_execution_budget(state)
    candidates = [
        issue for issue in state.get("review_issues") or []
        if issue.get("auto_fixable", False)
        and issue.get("fix_risk") == "low"
        and not issue.get("requires_human_confirmation", False)
    ]
    enabled = bool(candidates) and budget.can_consume(
        patch_attempts=1,
        model_calls=1,
        token_usage=4_096,
    )
    message = "存在可自动修复问题" if enabled else "没有可执行的自动修复"
    return ReviewState(
        phase=ReviewPhase.repair,
        repair_enabled=enabled,
        step_progress=append_step(state, "repair_policy", "completed", message),
    )


async def repair_generate_patch_node(state: ReviewState) -> ReviewState:
    action = AgentAction(
        action=AgentActionName.revise_patch,
        reason="修复策略创建候选补丁",
        target_issue_ids=[
            issue.get("id", "")
            for issue in state.get("review_issues") or []
            if issue.get("auto_fixable", False)
            and issue.get("fix_risk") == "low"
            and not issue.get("requires_human_confirmation", False)
        ],
    )
    return await patch_node(_with_action(state, action))


async def repair_apply_patch_node(state: ReviewState) -> ReviewState:
    patches_by_id = {
        item.get("id"): item
        for item in state.get("patches") or []
        if item.get("id")
    }
    patch_id = next(
        (
            candidate_id
            for candidate_id in state.get("pending_patch_ids") or []
            if patches_by_id.get(candidate_id, {}).get("status") == PatchStatus.generated.value
        ),
        None,
    )
    if patch_id is None:
        return ReviewState(
            phase=ReviewPhase.repair,
            repair_enabled=False,
            active_patch_id=None,
            active_patch_validation_passed=False,
            pending_patch_ids=[],
            step_progress=append_step(state, "patch_apply", "failed", "没有本轮可应用的候选补丁"),
        )
    action = AgentAction(
        action=AgentActionName.apply_patch,
        reason="在任务临时 clone 中应用本轮候选补丁",
        tool_args={"patch_id": patch_id},
    )
    result = await patch_node(_with_action(state, action))
    return ReviewState(
        **result,
        active_patch_id=patch_id,
        active_patch_validation_passed=None,
        pending_patch_ids=[
            candidate_id
            for candidate_id in state.get("pending_patch_ids") or []
            if candidate_id != patch_id
        ],
    )


async def repair_validation_node(state: ReviewState) -> ReviewState:
    active_patch_id = state.get("active_patch_id")
    active_patch = next(
        (item for item in state.get("patches") or [] if item.get("id") == active_patch_id),
        None,
    )
    if active_patch is None or active_patch.get("status") != PatchStatus.applied.value:
        await restore_patch_workspace(state)
        return ReviewState(
            phase=ReviewPhase.validation,
            repair_enabled=not state.get("validation_blocked", False),
            active_patch_validation_passed=False,
            step_progress=append_step(state, "patched_validation", "failed", "当前候选补丁未成功应用，跳过验证"),
        )
    return await patched_validation_node(state)


async def repair_assessment_node(state: ReviewState) -> ReviewState:
    """验证后恢复 repair 阶段，允许 Agent 仅作修订或放弃判断。"""
    active_patch_id = state.get("active_patch_id")
    active_patch = next(
        (item for item in state.get("patches") or [] if item.get("id") == active_patch_id),
        None,
    )
    has_verified_patch = (
        active_patch is not None
        and active_patch.get("status") in {
            PatchStatus.apply_failed.value,
            PatchStatus.validation_passed.value,
            PatchStatus.validation_failed.value,
        }
    )
    enabled = has_verified_patch and not state.get("validation_blocked", False)
    message = "补丁验证已完成" if has_verified_patch else "当前补丁未通过应用，修复流程已结束"
    return ReviewState(
        phase=ReviewPhase.repair,
        repair_enabled=enabled,
        step_progress=append_step(state, "repair_assessment", "completed", message),
    )


async def repair_accept_patch_node(state: ReviewState) -> ReviewState:
    """服务端独立验证接受条件，模型的 accept_patch 不能绕过此策略。"""
    allowed, reason = _can_accept_active_patch(state)
    active_patch_id = state.get("active_patch_id")
    patches = [PatchResult.model_validate(item) for item in state.get("patches") or []]
    if not allowed:
        for patch in patches:
            if patch.id == active_patch_id and patch.status != PatchStatus.validation_passed:
                patch.status = PatchStatus.abandoned
        return ReviewState(
            phase=ReviewPhase.repair,
            repair_enabled=False,
            patches=[patch.model_dump(mode="json") for patch in patches],
            agent_events=append_event(
                state, AgentActionName.accept_patch, reason, "rejected", "服务端拒绝接受候选补丁"
            ),
            step_progress=append_step(state, "repair_accept", "completed", f"拒绝接受补丁：{reason}"),
        )

    for patch in patches:
        if patch.id != active_patch_id and patch.status == PatchStatus.generated:
            patch.status = PatchStatus.abandoned
    return ReviewState(
        phase=ReviewPhase.repair,
        repair_enabled=False,
        patches=[patch.model_dump(mode="json") for patch in patches],
        agent_events=append_event(
            state, AgentActionName.accept_patch, reason, "completed", "服务端已接受通过验证的补丁"
        ),
        step_progress=append_step(state, "repair_accept", "completed", "补丁满足全部接受条件"),
    )


def _can_accept_active_patch(state: ReviewState) -> tuple[bool, str]:
    active_patch_id = state.get("active_patch_id")
    patch = next(
        (item for item in state.get("patches") or [] if item.get("id") == active_patch_id), None
    )
    if patch is None or patch.get("status") != PatchStatus.validation_passed.value:
        return False, "补丁未成功应用并通过验证"
    if state.get("validation_blocked") or state.get("patch_workspace_clean") is not True:
        return False, "验证策略阻断或工作树未确认恢复到干净 Head"
    delta = next(
        (item for item in reversed(state.get("validation_deltas") or [])
        if item.get("patch_id") == active_patch_id), None,
    )
    if delta is None or delta.get("introduced_failure"):
        return False, "缺少验证差异或存在新增失败"
    if not _patch_size_within_limit(patch.get("diff_content", "")):
        return False, "补丁规模超过受控限制"
    issue = next(
        (item for item in state.get("review_issues") or [] if item.get("id") == patch.get("issue_id")),
        None,
    )
    if not issue or not issue.get("auto_fixable") or issue.get("fix_risk") != "low":
        return False, "目标问题不是低风险自动修复项"
    if issue.get("requires_human_confirmation") or not issue.get("evidence"):
        return False, "目标问题缺少可自动接受的证据"
    resolved_failure = bool(delta.get("resolved_failure"))
    static_evidence = (
        f"+++ b/{issue.get('file_path')}" in patch.get("diff_content", "")
        and bool(state.get("static_results"))
        and all(item.get("passed", False) for item in state.get("static_results") or [])
    )
    if not (resolved_failure or static_evidence):
        return False, "目标失败未解决，且没有足够的静态修复证据"
    return True, "补丁已应用、无新增失败且目标问题已由验证或静态证据覆盖"


def _patch_size_within_limit(diff_content: str) -> bool:
    changed_lines = [
        line for line in diff_content.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    changed_files = sum(1 for line in diff_content.splitlines() if line.startswith("diff --git "))
    return bool(changed_lines) and len(changed_lines) <= 80 and changed_files <= 3


def _with_action(state: ReviewState, action: AgentAction) -> ReviewState:
    payload: dict[str, Any] = dict(state)
    payload["next_action"] = action.model_dump(mode="json")
    return ReviewState(**payload)


async def repair_abandon_patch_node(state: ReviewState) -> ReviewState:
    """显式结束未通过的候选补丁，避免其与最终有效补丁混淆。"""
    active_patch_id = state.get("active_patch_id")
    patches = [PatchResult.model_validate(item) for item in state.get("patches") or []]
    for patch in patches:
        if patch.id == active_patch_id and patch.status != PatchStatus.validation_passed:
            patch.status = PatchStatus.abandoned
        elif patch.status == PatchStatus.generated:
            patch.status = PatchStatus.abandoned
            break
    return ReviewState(
        phase=ReviewPhase.repair,
        repair_enabled=False,
        patches=[patch.model_dump(mode="json") for patch in patches],
        step_progress=append_step(state, "repair_abandon", "completed", "已结束当前候选补丁"),
    )
