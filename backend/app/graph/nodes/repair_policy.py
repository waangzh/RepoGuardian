"""确定性修复准入策略与修复子图适配节点。"""

from typing import Any

from app.graph.nodes._events import append_step
from app.graph.nodes.patch import patch_node
from app.graph.nodes.test import test_node
from app.graph.policies import get_execution_budget
from app.graph.state import ReviewState
from app.models.review import AgentAction, AgentActionName, ReviewPhase


async def repair_policy_node(state: ReviewState) -> ReviewState:
    """仅让标记为 auto_fixable 的问题进入补丁流程。"""
    budget = get_execution_budget(state)
    candidates = [
        issue for issue in state.get("review_issues") or [] if issue.get("auto_fixable", False)
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
        ],
    )
    return await patch_node(_with_action(state, action))


async def repair_apply_patch_node(state: ReviewState) -> ReviewState:
    action = AgentAction(
        action=AgentActionName.apply_patch,
        reason="在任务临时 clone 中应用最新候选补丁",
    )
    return await patch_node(_with_action(state, action))


async def repair_validation_node(state: ReviewState) -> ReviewState:
    action = AgentAction(
        action=AgentActionName.run_tests,
        reason="验证候选补丁",
    )
    result = await test_node(_with_action(state, action))
    result["phase"] = ReviewPhase.validation
    return ReviewState(**result)


async def repair_assessment_node(state: ReviewState) -> ReviewState:
    """验证后恢复 repair 阶段，允许 Agent 仅作修订或放弃判断。"""
    has_patch = any(item.get("status") == "applied" for item in state.get("patches") or [])
    return ReviewState(
        phase=ReviewPhase.repair,
        repair_enabled=has_patch,
        step_progress=append_step(state, "repair_assessment", "completed", "补丁验证已完成"),
    )


def _with_action(state: ReviewState, action: AgentAction) -> ReviewState:
    payload: dict[str, Any] = dict(state)
    payload["next_action"] = action.model_dump(mode="json")
    return ReviewState(**payload)
