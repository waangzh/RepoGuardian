"""运行级唯一模型消费账本。

Unit、Verifier 和跨 Unit 只是来源维度；真实 attempt 的预留、结算、未知
用量和保护义务全部在这里原子完成。持久化适配器通过 ``load``/``save``
注入，避免把进程对象写入 checkpoint。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4


class RunBudgetError(RuntimeError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class AttemptState(StrEnum):
    RESERVED = "reserved"
    DISPATCHING = "dispatching"
    RESPONSE_RECEIVED = "response_received"
    SETTLED = "settled"
    UNKNOWN = "unknown"
    RELEASED = "released"


class ObligationState(StrEnum):
    ACTIVE = "active"
    CONVERTED = "converted"
    RELEASED = "released"
    ABANDONED = "abandoned"


class RunBudgetStore(Protocol):
    def load(self, run_id: str) -> dict[str, Any] | None: ...
    def save(self, run_id: str, payload: dict[str, Any], revision: int) -> None: ...


class ReviewRepositoryRunBudgetStore:
    """把运行账本保存到现有 SideEffect 表，复用其 CAS 和 lease 边界。"""

    def __init__(self, repository: Any, lease: dict[str, Any] | None = None) -> None:
        self.repository = repository
        self.lease = lease

    def load(self, run_id: str) -> dict[str, Any] | None:
        return self.repository.load_run_budget(run_id)

    def save(self, run_id: str, payload: dict[str, Any], revision: int) -> None:
        self.repository.save_run_budget(run_id, payload, revision - 1, self.lease)


@dataclass
class AttemptRecord:
    attempt_id: str
    request_id: str
    run_id: str
    task_id: str | None
    operation: str
    request_hash: str
    estimated_tokens: int
    reserved_tokens: int
    output_tokens: int
    reserved_cost_microusd: int | None = None
    state: AttemptState = AttemptState.RESERVED
    actual_tokens: int | None = None
    released_tokens: int = 0
    overrun_tokens: int = 0
    usage_available: bool = False
    settled: bool = False
    sent: bool = False
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    settled_at: str | None = None


@dataclass
class ObligationRecord:
    obligation_id: str
    run_id: str
    owner_task_id: str
    reason: str
    token_budget: int
    request_hash: str | None = None
    state: ObligationState = ObligationState.ACTIVE
    created_revision: int = 0
    converted_attempt_id: str | None = None


def _dump(value: Any) -> Any:
    if isinstance(value, (AttemptState, ObligationState)):
        return value.value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "__dataclass_fields__"):
        return {key: _dump(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: _dump(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dump(item) for item in value]
    return value


class ReviewRunBudget:
    """可并发调用的运行预算；所有修改都在同一锁和 revision 下完成。"""

    version = "review-run-budget-v1"

    def __init__(self, run_id: str, *, max_tokens: int, max_calls: int,
                 max_cost_microusd: int | None = None, store: RunBudgetStore | None = None,
                 enforce: bool = True, revision: int = 0) -> None:
        if max_tokens < 0 or max_calls < 0 or (max_cost_microusd is not None and max_cost_microusd < 0):
            raise ValueError("run budget limits must not be negative")
        self.run_id = run_id
        self.max_tokens = max_tokens
        self.max_calls = max_calls
        self.max_cost_microusd = max_cost_microusd
        self.enforce = enforce
        self.revision = revision
        self.settled_actual = 0
        self.in_flight_reserved = 0
        self.unknown_reserved = 0
        self.calls_consumed = 0
        self.cost_settled_microusd = 0
        self.cost_reserved_microusd = 0
        self.attempts: dict[str, AttemptRecord] = {}
        self.obligations: dict[str, ObligationRecord] = {}
        self.store = store
        self._lock = asyncio.Lock()

    @property
    def protected_obligations(self) -> int:
        return sum(item.token_budget for item in self.obligations.values()
                   if item.state == ObligationState.ACTIVE)

    def available(self, *, obligation_id: str | None = None) -> dict[str, int]:
        own = 0
        if obligation_id and self.obligations.get(obligation_id):
            obligation = self.obligations[obligation_id]
            if obligation.state == ObligationState.ACTIVE:
                own = obligation.token_budget
        return {
            "tokens": max(0, self.max_tokens - self.settled_actual - self.in_flight_reserved
                           - self.unknown_reserved - self.protected_obligations + own),
            "calls": max(0, self.max_calls - self.calls_consumed),
        }

    def _persist_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "run_id": self.run_id,
            "limits": {"max_tokens": self.max_tokens, "max_calls": self.max_calls,
                        "max_cost_microusd": self.max_cost_microusd},
            "enforce": self.enforce,
            "revision": self.revision,
            "settled_actual": self.settled_actual,
            "in_flight_reserved": self.in_flight_reserved,
            "unknown_reserved": self.unknown_reserved,
            "calls_consumed": self.calls_consumed,
            "cost_settled_microusd": self.cost_settled_microusd,
            "cost_reserved_microusd": self.cost_reserved_microusd,
            "attempts": _dump(self.attempts),
            "obligations": _dump(self.obligations),
        }

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self._persist_payload())

    async def _persist(self) -> None:
        if self.store is not None:
            await asyncio.to_thread(self.store.save, self.run_id, self.snapshot(), self.revision)

    async def reserve(self, *, request_id: str, operation: str, estimated_tokens: int,
                      output_tokens: int = 0, task_id: str | None = None,
                      request_hash: str | None = None, obligation_id: str | None = None,
                      estimated_cost_microusd: int | None = None) -> AttemptRecord:
        if estimated_tokens < 0 or output_tokens < 0 or (estimated_cost_microusd is not None and estimated_cost_microusd < 0):
            raise ValueError("request reservation must not be negative")
        async with self._lock:
            if not self.enforce:
                available = self.available(obligation_id=obligation_id)
            else:
                available = self.available(obligation_id=obligation_id)
                if estimated_tokens > available["tokens"] or available["calls"] < 1:
                    details = {"request_id": request_id, "operation": operation,
                               "requested_tokens": estimated_tokens, **available,
                               "budget_revision": self.revision,
                               "protected_obligations": self.protected_obligations}
                    raise RunBudgetError("run_budget_admission_rejected", details=details)
            if (self.max_cost_microusd is not None and estimated_cost_microusd is not None
                    and self.cost_settled_microusd + self.cost_reserved_microusd + estimated_cost_microusd > self.max_cost_microusd):
                raise RunBudgetError("run_budget_cost_rejected", details={
                    "requested_cost_microusd": estimated_cost_microusd,
                    "available_cost_microusd": max(0, self.max_cost_microusd - self.cost_settled_microusd - self.cost_reserved_microusd),
                })
            attempt_id = uuid4().hex
            record = AttemptRecord(
                attempt_id=attempt_id, request_id=request_id, run_id=self.run_id,
                task_id=task_id, operation=operation,
                request_hash=request_hash or hashlib.sha256(request_id.encode()).hexdigest(),
                estimated_tokens=estimated_tokens, reserved_tokens=estimated_tokens,
                output_tokens=output_tokens,
                reserved_cost_microusd=estimated_cost_microusd,
            )
            self.attempts[attempt_id] = record
            self.in_flight_reserved += estimated_tokens
            self.cost_reserved_microusd += estimated_cost_microusd or 0
            self.calls_consumed += 1
            self.revision += 1
            await self._persist()
            return copy.deepcopy(record)

    async def mark_dispatching(self, attempt_id: str) -> AttemptRecord:
        async with self._lock:
            record = self._get(attempt_id)
            if record.state not in {AttemptState.RESERVED, AttemptState.DISPATCHING}:
                return copy.deepcopy(record)
            record.state = AttemptState.DISPATCHING
            record.sent = True
            self.revision += 1
            await self._persist()
            return copy.deepcopy(record)

    async def settle(self, attempt_id: str, *, actual_tokens: int | None,
                     cost_microusd: int | None = None) -> AttemptRecord:
        async with self._lock:
            record = self._get(attempt_id)
            if record.settled:
                return copy.deepcopy(record)
            self.in_flight_reserved = max(0, self.in_flight_reserved - record.reserved_tokens)
            self.cost_reserved_microusd = max(0, self.cost_reserved_microusd - (record.reserved_cost_microusd or 0))
            if actual_tokens is None:
                self.unknown_reserved += record.reserved_tokens
                record.state = AttemptState.UNKNOWN
                record.usage_available = False
            else:
                record.actual_tokens = actual_tokens
                record.usage_available = True
                record.released_tokens = max(0, record.reserved_tokens - actual_tokens)
                record.overrun_tokens = max(0, actual_tokens - record.reserved_tokens)
                self.settled_actual += actual_tokens
                record.state = AttemptState.SETTLED
                if cost_microusd is not None:
                    self.cost_settled_microusd += cost_microusd
            record.settled = True
            record.settled_at = datetime.now(timezone.utc).isoformat()
            self.revision += 1
            await self._persist()
            return copy.deepcopy(record)

    async def reconcile_unknown(self, attempt_id: str, *, actual_tokens: int | None,
                                cost_microusd: int | None = None) -> AttemptRecord:
        current = self.attempts.get(attempt_id)
        if current is None:
            raise RunBudgetError("unknown_attempt", details={"attempt_id": attempt_id})
        if not current.settled or current.state != AttemptState.UNKNOWN:
            return await self.settle(attempt_id, actual_tokens=actual_tokens,
                                      cost_microusd=cost_microusd)
        async with self._lock:
            record = self._get(attempt_id)
            if record.state != AttemptState.UNKNOWN:
                return copy.deepcopy(record)
            if actual_tokens is None:
                return copy.deepcopy(record)
            self.unknown_reserved = max(0, self.unknown_reserved - record.reserved_tokens)
            record.actual_tokens = actual_tokens
            record.usage_available = True
            record.released_tokens = max(0, record.reserved_tokens - actual_tokens)
            record.overrun_tokens = max(0, actual_tokens - record.reserved_tokens)
            record.state = AttemptState.SETTLED
            if cost_microusd is not None:
                self.cost_settled_microusd += cost_microusd
            self.settled_actual += actual_tokens
            self.revision += 1
            await self._persist()
            return copy.deepcopy(record)

    async def release_unsent(self, attempt_id: str, *, reason: str = "not_sent") -> AttemptRecord:
        async with self._lock:
            record = self._get(attempt_id)
            if record.state == AttemptState.RELEASED:
                return copy.deepcopy(record)
            if record.sent or record.state not in {AttemptState.RESERVED, AttemptState.RELEASED}:
                raise RunBudgetError("attempt_already_dispatching", details={"attempt_id": attempt_id})
            self.in_flight_reserved = max(0, self.in_flight_reserved - record.reserved_tokens)
            self.cost_reserved_microusd = max(0, self.cost_reserved_microusd - (record.reserved_cost_microusd or 0))
            self.calls_consumed = max(0, self.calls_consumed - 1)
            record.released_tokens = record.reserved_tokens
            record.state = AttemptState.RELEASED
            record.settled = True
            record.settled_at = datetime.now(timezone.utc).isoformat()
            self.revision += 1
            await self._persist()
            return copy.deepcopy(record)

    async def create_obligation(self, *, owner_task_id: str, token_budget: int,
                                reason: str, request_hash: str | None = None) -> ObligationRecord:
        if token_budget < 0:
            raise ValueError("obligation token budget must not be negative")
        async with self._lock:
            if token_budget > self.available()["tokens"]:
                raise RunBudgetError("run_budget_obligation_rejected", details={
                    "requested_tokens": token_budget, **self.available()})
            item = ObligationRecord(uuid4().hex, self.run_id, owner_task_id, reason,
                                    token_budget, request_hash, created_revision=self.revision)
            self.obligations[item.obligation_id] = item
            self.revision += 1
            await self._persist()
            return copy.deepcopy(item)

    async def release_obligation(self, obligation_id: str, *, abandoned: bool = False) -> ObligationRecord:
        async with self._lock:
            item = self.obligations.get(obligation_id)
            if item is None:
                raise RunBudgetError("unknown_obligation", details={"obligation_id": obligation_id})
            if item.state == ObligationState.ACTIVE:
                item.state = ObligationState.ABANDONED if abandoned else ObligationState.RELEASED
                self.revision += 1
                await self._persist()
            return copy.deepcopy(item)

    async def convert_obligation(self, obligation_id: str, *, request_id: str, operation: str,
                                 estimated_tokens: int, output_tokens: int = 0,
                                 task_id: str | None = None, request_hash: str | None = None) -> AttemptRecord:
        async with self._lock:
            item = self.obligations.get(obligation_id)
            if item is None or item.state != ObligationState.ACTIVE:
                raise RunBudgetError("obligation_not_convertible", details={"obligation_id": obligation_id})
            available = self.available(obligation_id=obligation_id)
            if estimated_tokens > available["tokens"] or available["calls"] < 1:
                raise RunBudgetError("run_budget_admission_rejected", details={"obligation_id": obligation_id,
                    "requested_tokens": estimated_tokens, **available})
            # Convert in this same critical section: the protected tokens are not deducted twice.
            item.state = ObligationState.CONVERTED
            record = AttemptRecord(uuid4().hex, request_id, self.run_id, task_id, operation,
                                   request_hash or hashlib.sha256(request_id.encode()).hexdigest(),
                                   estimated_tokens, estimated_tokens, output_tokens)
            item.converted_attempt_id = record.attempt_id
            self.attempts[record.attempt_id] = record
            self.in_flight_reserved += estimated_tokens
            self.calls_consumed += 1
            self.revision += 1
            await self._persist()
            return copy.deepcopy(record)

    def _get(self, attempt_id: str) -> AttemptRecord:
        try:
            return self.attempts[attempt_id]
        except KeyError as exc:
            raise RunBudgetError("unknown_attempt", details={"attempt_id": attempt_id}) from exc

    @classmethod
    def restore(cls, payload: dict[str, Any], *, store: RunBudgetStore | None = None,
                recover_running: bool = True) -> "ReviewRunBudget":
        limits = payload.get("limits") or {}
        budget = cls(payload["run_id"], max_tokens=int(limits.get("max_tokens", 0)),
                     max_calls=int(limits.get("max_calls", 0)),
                     max_cost_microusd=limits.get("max_cost_microusd"),
                     store=store, enforce=bool(payload.get("enforce", True)),
                     revision=int(payload.get("revision", 0)))
        for key in ("settled_actual", "in_flight_reserved", "unknown_reserved", "calls_consumed",
                    "cost_settled_microusd", "cost_reserved_microusd"):
            setattr(budget, key, int(payload.get(key, 0)))
        for key, raw in (payload.get("attempts") or {}).items():
            item = dict(raw)
            item["state"] = AttemptState(item["state"])
            budget.attempts[key] = AttemptRecord(**item)
        for key, raw in (payload.get("obligations") or {}).items():
            item = dict(raw)
            item["state"] = ObligationState(item["state"])
            budget.obligations[key] = ObligationRecord(**item)
        if recover_running:
            # Conservative recovery: a crash at the send boundary is unknown, never free.
            for item in budget.attempts.values():
                if item.state in {AttemptState.RESERVED, AttemptState.DISPATCHING} and not item.settled:
                    item.state = AttemptState.UNKNOWN
                    item.settled = True
                    item.sent = True
                    budget.in_flight_reserved = max(0, budget.in_flight_reserved - item.reserved_tokens)
                    budget.unknown_reserved += item.reserved_tokens
            budget.revision += 1
        return budget

    @classmethod
    async def load_or_create(cls, run_id: str, *, max_tokens: int, max_calls: int,
                             store: RunBudgetStore | None = None,
                             max_cost_microusd: int | None = None,
                             enforce: bool = True, recover_running: bool = False) -> "ReviewRunBudget":
        payload = await asyncio.to_thread(store.load, run_id) if store is not None else None
        if payload:
            return cls.restore(payload, store=store, recover_running=recover_running)
        return cls(run_id, max_tokens=max_tokens, max_calls=max_calls,
                   max_cost_microusd=max_cost_microusd, store=store, enforce=enforce)


_active_run_budget: ContextVar[ReviewRunBudget | None] = ContextVar("active_review_run_budget", default=None)


def current_run_budget() -> ReviewRunBudget | None:
    return _active_run_budget.get()


@contextmanager
def activate_run_budget(budget: ReviewRunBudget):
    token = _active_run_budget.set(budget)
    try:
        yield budget
    finally:
        _active_run_budget.reset(token)
