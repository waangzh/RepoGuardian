"""Whole-review reservations survive failures, replay, and branch retries."""

import asyncio
from uuid import uuid4


class ReviewBudgetExceeded(RuntimeError):
    pass


def budget_summary(rows, limits):
    reserved = sum(row["reserved_input_tokens"] for row in rows)
    used = sum(row["input_tokens"] if row["input_tokens"] is not None
               else row["reserved_input_tokens"] for row in rows)
    known = all(row["input_tokens"] is not None for row in rows)
    return {
        "model_calls": len(rows), "model_call_limit": limits.total_model_calls,
        "input_tokens": sum(row["input_tokens"] for row in rows) if known else None,
        "reserved_input_tokens": reserved, "committed_input_tokens": used,
        "input_token_limit": limits.total_input_tokens,
        "output_tokens": sum(row["output_tokens"] for row in rows)
            if all(row["output_tokens"] is not None for row in rows) else None,
        "cached_input_tokens": sum(row["cached_input_tokens"] for row in rows)
            if all(row["cached_input_tokens"] is not None for row in rows) else None,
        "unknown_calls": sum(row["input_tokens"] is None for row in rows),
    }


class ReviewBudget:
    def __init__(self, limits, *, repository=None, task_id=None, lease=None, active=None):
        self.limits, self.repository, self.task_id, self.lease = limits, repository, task_id, lease
        self.active = active
        self.rows = {}
        self.settled = set()
        self.lock = asyncio.Lock()

    async def reserve(self, agent_id, estimate):
        if self.active:
            await self.active()
        if self.repository:
            return await asyncio.to_thread(self.repository.reserve_model_call,
                self.task_id, agent_id, estimate, self.limits, lease=self.lease)
        async with self.lock:
            if type(estimate) is not int or estimate < 1:
                raise ValueError("invalid_budget_reservation")
            summary = budget_summary(list(self.rows.values()), self.limits)
            if (summary["model_calls"] >= self.limits.total_model_calls
                    or summary["committed_input_tokens"] + estimate > self.limits.total_input_tokens):
                raise ReviewBudgetExceeded("review_budget_exhausted")
            identity = uuid4().hex
            self.rows[identity] = {"reserved_input_tokens": estimate, "input_tokens": None,
                                   "output_tokens": None, "cached_input_tokens": None}
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
            self.settled.add(identity)

    async def summary(self):
        if self.repository:
            return await asyncio.to_thread(self.repository.model_budget_summary, self.task_id, self.limits)
        async with self.lock:
            return budget_summary(list(self.rows.values()), self.limits)
