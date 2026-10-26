"""Read-only eligibility for continuing a frozen review; never resets its ledger."""

import time

from pydantic import ValidationError

from app.models.review_runtime import TaskRuntimeReport
from app.services.review.planning.review_plan import remaining_review_seconds
from app.services.review.review_run_budget import ReviewRunBudget

ALL_PENDING = "__all_pending__"


def continuation(task, checkpoint, ledger):
    result = {"review_id": task.id, "can_resume": False, "unit_ids": [], "reason": "checkpoint_missing"}
    if task.status.value == "cancelled":
        return {**result, "reason": "cancelled"}
    if not checkpoint or not ledger or checkpoint.get("spec", {}).get("run_id") != task.id:
        return result
    try:
        report = TaskRuntimeReport.model_validate(task.task_runtime_report)
        budget = ReviewRunBudget.restore(ledger, recover_running=False)
    except (ValidationError, ValueError, KeyError, TypeError):
        return result
    if budget.run_id != task.id:
        return result
    if checkpoint.get("cancelled"):
        return {**result, "reason": "cancelled"}
    if report.unknown_attempts or any(item.state.value in {"unknown", "dispatching", "response_received", "reserved"}
                                    for item in budget.attempts.values()):
        return {**result, "reason": "unknown_attempts"}
    if any(item.get("state") in {"unknown", "result_missing"} for item in report.work_items):
        return {**result, "reason": "unknown_attempts"}
    config = task.task_runtime_configuration or {}
    if not isinstance(config.get("deadline_seconds"), (int, float)):
        return {**result, "reason": "checkpoint_missing"}
    remaining = remaining_review_seconds(config, task.created_at.isoformat())
    result.update(calls_consumed=budget.calls_consumed, tokens_consumed=budget.settled_actual,
                  max_calls=budget.max_calls, max_tokens=budget.max_tokens,
                  remaining_seconds=max(0, int(remaining)))
    deadline = checkpoint.get("deadline_at")
    if remaining <= 0 or not isinstance(deadline, (int, float)) or deadline <= time.time():
        return {**result, "reason": "deadline"}
    if (budget.calls_consumed >= budget.max_calls or budget.settled_actual + budget.unknown_reserved >= budget.max_tokens
            or (budget.max_cost_microusd is not None and budget.cost_settled_microusd
                + budget.cost_reserved_microusd >= budget.max_cost_microusd)):
        return {**result, "reason": "budget_exhausted"}
    gaps = {item.get("goal_id") for item in report.initial_scope_coverage if item.get("status") != "examined"}
    investigations = {item.get("work_item_id") for item in report.admitted_investigations
                      if item.get("status") != "answered"}
    units = sorted({item["unit_id"] for item in report.work_items if item.get("unit_id")
        and (gaps.intersection(item.get("goal_ids", [])) or item.get("work_item_id") in investigations
             or item.get("retryable_gap") is True)})
    return {**result, "can_resume": bool(units), "unit_ids": units,
            "reason": "available" if units else "no_retryable_gap"}
