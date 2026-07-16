"""审查阶段、Agent 动作和执行预算的服务端策略。"""

from typing import Any

from app.models.review import AgentAction, AgentActionName, ExecutionBudget, ReviewPhase


ALLOWED_ACTIONS_BY_PHASE: dict[ReviewPhase, frozenset[AgentActionName]] = {
    ReviewPhase.discovery: frozenset({
        AgentActionName.retrieve_context,
        AgentActionName.review_code,
        AgentActionName.request_human,
    }),
    ReviewPhase.repair: frozenset({
        AgentActionName.revise_patch,
        AgentActionName.accept_patch,
        AgentActionName.abandon_patch,
        AgentActionName.request_human,
    }),
}


class ActionPolicyViolation(ValueError):
    """Agent 尝试在当前阶段执行不受允许的动作。"""


def get_phase(state: dict[str, Any]) -> ReviewPhase:
    """读取阶段，并为尚未迁移的调用方提供 prepare 兼容默认值。"""
    return ReviewPhase(state.get("phase") or ReviewPhase.prepare)


def get_execution_budget(state: dict[str, Any]) -> ExecutionBudget:
    """将图状态中的预算统一还原为领域模型。"""
    value = state.get("execution_budget")
    if isinstance(value, ExecutionBudget):
        return value
    return ExecutionBudget.model_validate(value or {})


def consume_budget(state: dict[str, Any], **amounts: int) -> ExecutionBudget | None:
    """原子检查并消耗预算；超限时返回 None。"""
    budget = get_execution_budget(state)
    if not budget.can_consume(**amounts):
        return None
    return budget.consume(**amounts)


def validate_action_for_phase(phase: ReviewPhase, action: AgentAction) -> None:
    """拒绝当前阶段以外的任何 Agent 动作。"""
    allowed = ALLOWED_ACTIONS_BY_PHASE.get(phase, frozenset())
    if action.action not in allowed:
        raise ActionPolicyViolation(
            f"action '{action.action.value}' is not allowed during phase '{phase.value}'"
        )


def safe_action_for_phase(phase: ReviewPhase, reason: str) -> AgentAction:
    """策略拒绝或预算耗尽时使用的确定性安全收敛动作。"""
    if phase == ReviewPhase.discovery:
        return AgentAction(action=AgentActionName.review_code, reason=reason)
    if phase == ReviewPhase.repair:
        return AgentAction(action=AgentActionName.abandon_patch, reason=reason)
    raise ActionPolicyViolation(f"no safe agent action is defined for phase '{phase.value}'")
