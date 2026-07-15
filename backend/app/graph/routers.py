"""仅基于受控动作返回固定图节点名的路由函数。"""

from typing import Any

from app.graph.policies import get_phase, validate_action_for_phase
from app.models.review import AgentAction, AgentActionName, ReviewPhase


def _read_action(state: dict[str, Any]) -> AgentAction | None:
    raw = state.get("next_action")
    if not raw:
        return None
    try:
        return AgentAction.model_validate(raw)
    except ValueError:
        return None


def route_discovery_action(state: dict[str, Any]) -> str:
    """发现阶段只能回到检索，或进入固定的诊断节点。"""
    action = _read_action(state)
    if action is None:
        return "review"
    try:
        validate_action_for_phase(ReviewPhase.discovery, action)
    except ValueError:
        return "review"
    if action.action == AgentActionName.retrieve_context:
        return "context_retrieve"
    return "review"


def route_repair_action(state: dict[str, Any]) -> str:
    """修复阶段只能修订候选补丁，或安全放弃后返回主图。"""
    action = _read_action(state)
    if action is None:
        return "repair_exit"
    try:
        validate_action_for_phase(ReviewPhase.repair, action)
    except ValueError:
        return "repair_exit"
    if action.action == AgentActionName.revise_patch:
        return "generate_patch"
    return "repair_exit"


def route_repair_entry(state: dict[str, Any]) -> str:
    """修复策略仅在存在可自动修复问题且预算可用时创建补丁。"""
    return "generate_patch" if state.get("repair_enabled") else "repair_exit"


def route_agent_action(state: dict[str, Any]) -> str:
    """旧路由入口兼容层；新图不得使用此通用路由。"""
    action = _read_action(state)
    if action is None:
        return AgentActionName.finish_report.value
    phase = get_phase(state)
    if phase in {ReviewPhase.discovery, ReviewPhase.repair}:
        try:
            validate_action_for_phase(phase, action)
        except ValueError:
            return AgentActionName.finish_report.value
    return action.action.value
