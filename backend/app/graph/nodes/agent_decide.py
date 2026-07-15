"""仅在 discovery 与 repair 阶段调用模型的受限 Agent 决策节点。"""

import logging
from typing import Any

from app.agents.providers import LLMProviderError, build_provider
from app.core.config import settings
from app.graph.nodes._events import append_event, append_step
from app.graph.policies import (
    ActionPolicyViolation,
    consume_budget,
    get_execution_budget,
    get_phase,
    safe_action_for_phase,
    validate_action_for_phase,
)
from app.graph.state import ReviewState
from app.models.review import AgentAction, ReviewPhase

logger = logging.getLogger("RepoGuardian.Node")

_DECISION_TOKEN_RESERVE = 1_200


async def agent_decide_node(state: ReviewState) -> ReviewState:
    """从阶段白名单中选择动作，永不接受模型给出的自由节点名。"""
    phase = get_phase(state)
    if phase not in {ReviewPhase.discovery, ReviewPhase.repair}:
        raise ActionPolicyViolation(f"agent decisions are not permitted during '{phase.value}'")

    budget = get_execution_budget(state)
    if phase == ReviewPhase.discovery and not state.get("changed_files"):
        action = safe_action_for_phase(phase, "没有变更文件，无需额外上下文。")
        return _with_action(state, action, "completed", action.reason)
    if phase == ReviewPhase.discovery and not budget.can_consume(context_retrievals=1):
        action = safe_action_for_phase(phase, "上下文检索预算已耗尽，开始诊断。")
        return _with_action(state, action, "completed", action.reason)
    if not budget.can_consume(model_calls=1, token_usage=_DECISION_TOKEN_RESERVE):
        action = safe_action_for_phase(phase, "模型调用预算已耗尽，使用安全收敛动作。")
        return _with_action(state, action, "completed", action.reason)

    provider: Any = state.get("_provider") or build_provider(
        settings.repoguardian_provider,
        settings.openai_api_key,
        settings.openai_base_url,
        settings.repoguardian_model,
    )
    consumed = consume_budget(state, model_calls=1, token_usage=_DECISION_TOKEN_RESERVE)
    assert consumed is not None

    try:
        action = await provider.decide(dict(state), state.get("model"))
        validate_action_for_phase(phase, action)
    except (LLMProviderError, ValueError, ActionPolicyViolation) as exc:
        action = safe_action_for_phase(phase, f"已拒绝无效 Agent 动作：{exc}")
        return _with_action(
            state,
            action,
            "rejected",
            str(exc),
            execution_budget=consumed,
        )

    return _with_action(
        state,
        action,
        "selected",
        action.reason,
        execution_budget=consumed,
    )


def _with_action(
    state: ReviewState,
    action: AgentAction,
    status: str,
    message: str,
    *,
    execution_budget: Any | None = None,
) -> ReviewState:
    """将已经过阶段策略校验的动作、预算和审计事件写入状态。"""
    result: dict[str, Any] = {
        "next_action": action.model_dump(mode="json"),
        "agent_events": append_event(state, action.action, action.reason, status, message),
        "step_progress": append_step(
            state,
            f"agent:{action.action.value}",
            "completed",
            action.reason,
        ),
    }
    if execution_budget is not None:
        result["execution_budget"] = execution_budget.model_dump()
    return ReviewState(**result)
