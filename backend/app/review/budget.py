"""Whole-review reservations survive failures, replay, and branch retries."""

import asyncio
import logging
from uuid import uuid4

logger = logging.getLogger("RepoGuardian.Review")


class ReviewBudgetExceeded(RuntimeError):
    pass


def input_estimate(encoded_bytes, factor):
    """UTF-8 bytes are a stable proxy, not tokens; the factor is calibrated from usage."""
    return max(1, round(encoded_bytes * factor))


def branch_budget():
    return {"held_input_tokens": 0, "held_model_calls": 0,
            "finalizing": False, "final_requests": 0}


def budget_summary(rows, limits, branches=()):
    measured = [row for row in rows if row["input_tokens"] is not None]
    # 最差已观测比值：字节数无法换算 Token，只有实际 usage 能校准。
    factor = round(max((row["input_tokens"] / row["reserved_input_bytes"] for row in measured
                        if row["input_tokens"] > 0 and row["reserved_input_bytes"] > 0),
                       default=1.0), 3)
    held = sum(branch["held_input_tokens"] for branch in branches)
    outstanding = sum(input_estimate(row["reserved_input_bytes"], factor)
                      for row in rows if row["input_tokens"] is None) + held
    return {
        "model_calls": len(rows), "model_call_limit": limits.total_model_calls,
        "input_tokens": sum(row["input_tokens"] for row in measured)
            if len(measured) == len(rows) else None,
        "reserved_input_bytes": sum(row["reserved_input_bytes"] for row in rows),
        "committed_input_tokens": sum(row["input_tokens"] for row in measured) + outstanding,
        "outstanding_input_tokens": outstanding, "input_token_factor": factor,
        "held_input_tokens": held,
        "held_model_calls": sum(branch["held_model_calls"] for branch in branches),
        "input_token_limit": limits.total_input_tokens,
        "output_tokens": sum(row["output_tokens"] for row in rows)
            if all(row["output_tokens"] is not None for row in rows) else None,
        "cached_input_tokens": sum(row["cached_input_tokens"] for row in rows)
            if all(row["cached_input_tokens"] is not None for row in rows) else None,
        "unknown_calls": len(rows) - len(measured),
    }


def enforce_budget(summary, estimate, held, limits, stage, *, held_calls=0,
                   review_id="", agent_id=""):
    if summary["model_calls"] + summary["held_model_calls"] + 1 + held_calls > limits.total_model_calls:
        dimension = "model_calls"
    elif summary["committed_input_tokens"] + estimate + held > limits.total_input_tokens:
        dimension = "input_tokens"
    else:
        return
    logger.warning("预算拒绝 dimension=%s stage=%s model_calls=%s/%s committed_input=%s "
        "outstanding_input=%s estimate=%s held=%s factor=%s input_limit=%s "
        "held_calls=%s review_id=%s agent_id=%s", dimension, stage,
        summary["model_calls"], summary["model_call_limit"], summary["committed_input_tokens"],
        summary["outstanding_input_tokens"], estimate, held, summary["input_token_factor"],
        summary["input_token_limit"], summary["held_model_calls"] + held_calls, review_id, agent_id)
    raise ReviewBudgetExceeded(dimension)


def plan_reservation(rows, branches, agent_id, encoded_bytes, limits, *, stage, hold, review_id=""):
    if (type(encoded_bytes) is not int or encoded_bytes < 1
            or type(hold) is not int or not 0 <= hold <= 2
            or stage not in {"explore", "finalize"}):
        raise ValueError("invalid_budget_reservation")
    branch = dict(branches.get(agent_id, branch_budget()))
    if branch["finalizing"] and stage == "explore":
        raise ValueError("branch_already_finalizing")
    if stage == "finalize":
        if branch["final_requests"] >= 2:
            raise ReviewBudgetExceeded("finalization_attempts")
        # The correction consumes the remaining reservation, without reserving a third attempt.
        hold = min(hold, max(0, 1 - branch["final_requests"]))
        branch["finalizing"] = True
        branch["final_requests"] += 1
    # Convert this owner's hold to a request and its remaining hold in one transaction.
    summary = budget_summary(rows, limits, [value for key, value in branches.items() if key != agent_id])
    estimate = input_estimate(encoded_bytes, summary["input_token_factor"])
    enforce_budget(summary, estimate, estimate * hold, limits, stage, held_calls=hold,
                   review_id=review_id, agent_id=agent_id)
    branch.update(held_input_tokens=estimate * hold, held_model_calls=hold)
    return branch


class ReviewBudget:
    def __init__(self, limits, *, repository=None, task_id=None, lease=None, active=None):
        self.limits, self.repository, self.task_id, self.lease = limits, repository, task_id, lease
        self.active = active
        self.rows = {}
        self.branches = {}
        self.settled = set()
        self.lock = asyncio.Lock()

    async def reserve(self, agent_id, encoded_bytes, *, stage="explore", hold=0):
        if self.active:
            await self.active()
        if self.repository:
            return await asyncio.to_thread(self.repository.reserve_model_call,
                self.task_id, agent_id, encoded_bytes, self.limits, lease=self.lease,
                stage=stage, hold=hold)
        async with self.lock:
            branch = plan_reservation(list(self.rows.values()), self.branches, agent_id,
                encoded_bytes, self.limits, stage=stage, hold=hold, review_id=self.task_id or "")
            identity = uuid4().hex
            self.rows[identity] = {"reserved_input_bytes": encoded_bytes,
                                   "input_tokens": None,
                                   "output_tokens": None, "cached_input_tokens": None,
                                   "settled": False}
            self.branches[agent_id] = branch
            return identity

    async def settle(self, identity, usage):
        if self.repository:
            await asyncio.to_thread(self.repository.settle_model_call,
                                    self.task_id, identity, usage, lease=self.lease)
            return
        async with self.lock:
            if identity in self.settled:
                return
            if usage:
                if any(type(usage.get(key)) is not int or usage[key] < 0
                       for key in ("input_tokens", "output_tokens")):
                    raise ValueError("invalid_model_usage")
                self.rows[identity].update(input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                    cached_input_tokens=usage.get("input_token_details", {}).get("cache_read"))
            self.rows[identity]["settled"] = True
            self.settled.add(identity)

    async def summary(self):
        if self.repository:
            return await asyncio.to_thread(self.repository.model_budget_summary, self.task_id, self.limits)
        async with self.lock:
            return budget_summary(list(self.rows.values()), self.limits, self.branches.values())

    async def branch_state(self, agent_id):
        if self.repository:
            return await asyncio.to_thread(self.repository.model_branch_budget, self.task_id, agent_id)
        async with self.lock:
            return dict(self.branches.get(agent_id, branch_budget()))

    async def finish(self, agent_id):
        if self.repository:
            await asyncio.to_thread(self.repository.release_branch_budget,
                                    self.task_id, agent_id, lease=self.lease)
            return
        async with self.lock:
            if agent_id in self.branches:
                self.branches[agent_id].update(held_input_tokens=0, held_model_calls=0)
