from datetime import datetime, timezone
from typing import Any

from app.models.review import AgentActionName


def append_step(state: dict[str, Any], node: str, status: str, message: str) -> list[dict[str, Any]]:
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
