"""可观测性辅助函数：以不可变方式向 state 追加步骤/事件记录。

核心原则：不原地修改 state 中的 list，返回新 list，
调用方在 ReviewState(**result) 时用新值覆盖旧值。
"""

from datetime import datetime, timezone
from typing import Any

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
