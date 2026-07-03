"""可观测性辅助函数：以不可变方式向 state 追加步骤/事件记录。

核心原则：不原地修改 state 中的 list，返回新 list，
调用方在 ReviewState(**result) 时用新值覆盖旧值。
"""

from datetime import datetime, timezone
from typing import Any

from app.models.review import AgentActionName


def append_step(state: dict[str, Any], node: str, status: str, message: str) -> list[dict[str, Any]]:
    """向 step_progress 列表追加一个步骤记录（含时间戳），返回新列表。"""
    steps: list[dict[str, Any]] = list(state.get("step_progress") or [])
    steps.append({
        "node": node,
        "status": status,
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return steps


def append_event(
    state: dict[str, Any],
    action: AgentActionName | str,
    reason: str,
    status: str,
    message: str | None = None,
) -> list[dict[str, Any]]:
    """向 agent_events 列表追加一个 Agent 决策事件，返回新列表。"""
    events: list[dict[str, Any]] = list(state.get("agent_events") or [])
    action_value = action.value if isinstance(action, AgentActionName) else action
    events.append({
        "action": action_value,
        "reason": reason,
        "status": status,
        "message": message,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return events
