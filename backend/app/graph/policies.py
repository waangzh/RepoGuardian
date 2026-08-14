"""审查阶段、Agent 动作和执行预算的服务端策略。"""

from dataclasses import dataclass
from typing import Any

from app.models.review import AgentAction, AgentActionName, ExecutionBudget, ReviewPhase


@dataclass(frozen=True, slots=True)
class UnitActionRegistration:
    """Review Unit 动作的服务端唯一注册信息。"""

    action: AgentActionName
    route: str
    prompt_instruction: str


UNIT_ACTION_REGISTRY: tuple[UnitActionRegistration, ...] = (
    UnitActionRegistration(
        action=AgentActionName.retrieve_context,
        route="execute_read_tool",
        prompt_instruction=(
            "Use only for bounded read-only context retrieval. tool_args must be exactly "
            "{\"plan\": {...}}; the plan fields are reason, target_files, target_symbols, "
            "search_terms, relevance_types, include_callers, include_callees, include_tests, "
            "max_results, and depth. relevance_types must contain one or more of direct, caller, "
            "callee, test, module_config, text, adjacent, type_definition, import_source, or "
            "failure_location. Use target_files; never use files or file_requests."
        ),
    ),
    UnitActionRegistration(
        action=AgentActionName.file_read,
        route="execute_read_tool",
        prompt_instruction=(
            "Read an exact bounded line range from one readable file. tool_args must be exactly "
            "{\"request\": {\"file_path\": \"...\", \"start_line\": 1, \"end_line\": 80}}. "
            "The server clamps the range to max_lines_per_read."
        ),
    ),
    UnitActionRegistration(
        action=AgentActionName.file_find,
        route="execute_read_tool",
        prompt_instruction=(
            "Find a literal path fragment inside readable_files only. tool_args must be exactly "
            "{\"request\": {\"query\": \"literal\", \"max_results\": 12}}. Regex and traversal "
            "are not supported."
        ),
    ),
    UnitActionRegistration(
        action=AgentActionName.file_read_diff,
        route="execute_read_tool",
        prompt_instruction=(
            "Read already parsed diff hunks for one changed Unit file. tool_args must be exactly "
            "{\"request\": {\"file_path\": \"...\", \"hunk_ids\": []}}. Revisions and free-form "
            "patch text are not accepted."
        ),
    ),
    UnitActionRegistration(
        action=AgentActionName.report_issue,
        route="report_issue",
        prompt_instruction="Use once the available evidence is sufficient to run issue reporting.",
    ),
    UnitActionRegistration(
        action=AgentActionName.task_done,
        route="finish_unit",
        prompt_instruction=(
            "Use to finish the Review Unit after issue reporting, or when no clear issue exists. "
            "Zero reported issues is valid."
        ),
    ),
    UnitActionRegistration(
        action=AgentActionName.request_human,
        route="finish_unit",
        prompt_instruction=(
            "Use only when business rules are unavailable, multiple behaviors are safe, evidence "
            "is insufficient, or a security, funds, permission, or data-migration decision needs "
            "approval. Include human_request with missing_information, known_evidence, questions, "
            "and prohibited_operations."
        ),
    ),
)

UNIT_ALLOWED_ACTIONS = frozenset(item.action for item in UNIT_ACTION_REGISTRY)
UNIT_ACTION_ROUTES = {item.action.value: item.route for item in UNIT_ACTION_REGISTRY}


def render_unit_action_protocol() -> str:
    """从注册表生成模型可见的 Unit 动作协议。"""
    return "\n".join(
        f"- {item.action.value}: {item.prompt_instruction}"
        for item in UNIT_ACTION_REGISTRY
    )


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
