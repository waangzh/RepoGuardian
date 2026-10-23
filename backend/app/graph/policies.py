"""主图共享的执行预算策略。"""

from typing import Any

from app.models.review import ExecutionBudget


def get_execution_budget(state: dict[str, Any]) -> ExecutionBudget:
    """将图状态中的预算统一还原为领域模型。"""
    value = state.get("execution_budget")
    if isinstance(value, ExecutionBudget):
        return value
    return ExecutionBudget.model_validate(value or {})
