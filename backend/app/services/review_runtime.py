"""确定性审查任务队列和预算暂缓恢复。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Awaitable, Callable
import asyncio


class ReviewTaskStatus(StrEnum):
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    DEFERRED_BUDGET = "deferred_budget"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NOT_EXECUTED = "not_executed"


@dataclass
class ReviewTask:
    task_id: str
    operation: str
    priority: tuple[Any, ...] = field(default_factory=tuple)
    payload: Any = None
    status: ReviewTaskStatus = ReviewTaskStatus.READY
    reason: str | None = None
    last_checked_budget_revision: int = -1
    last_checked_obligation_revision: int = -1
    attempts: int = 0


class DeferredTaskQueue:
    """只在预算/义务 revision 变化后重查，避免释放前的忙轮询。"""

    def __init__(self) -> None:
        self._tasks: dict[str, ReviewTask] = {}

    def defer(self, task: ReviewTask, *, reason: str, budget_revision: int,
              obligation_revision: int = 0) -> ReviewTask:
        task.status = ReviewTaskStatus.DEFERRED_BUDGET
        task.reason = reason
        task.last_checked_budget_revision = budget_revision
        task.last_checked_obligation_revision = obligation_revision
        self._tasks[task.task_id] = task
        return task

    def pending(self) -> list[ReviewTask]:
        return sorted((task for task in self._tasks.values()
                       if task.status == ReviewTaskStatus.DEFERRED_BUDGET),
                      key=lambda task: (task.priority, task.task_id))

    def remove(self, task_id: str) -> None:
        self._tasks.pop(task_id, None)

    def snapshot(self) -> list[dict[str, Any]]:
        return [{"task_id": task.task_id, "operation": task.operation,
                 "status": task.status.value, "reason": task.reason,
                 "last_checked_budget_revision": task.last_checked_budget_revision,
                 "last_checked_obligation_revision": task.last_checked_obligation_revision,
                 "attempts": task.attempts} for task in self.pending()]

    async def drain(self, *, budget_revision: int, obligation_revision: int = 0,
                    admit: Callable[[ReviewTask], Any],
                    execute: Callable[[ReviewTask], Awaitable[Any]],
                    max_tasks: int | None = None) -> list[ReviewTask]:
        """重查可恢复任务；仍不可执行的任务保留在队列中。"""

        completed: list[ReviewTask] = []
        for task in self.pending()[:max_tasks]:
            if (task.last_checked_budget_revision == budget_revision
                    and task.last_checked_obligation_revision == obligation_revision):
                continue
            task.last_checked_budget_revision = budget_revision
            task.last_checked_obligation_revision = obligation_revision
            decision = admit(task)
            admitted = (decision.get("admitted", False) if isinstance(decision, dict)
                        else bool(getattr(decision, "admitted", False)))
            if not admitted:
                reason = (decision.get("reason") if isinstance(decision, dict)
                          else getattr(decision, "reason", None))
                task.reason = reason or task.reason
                continue
            task.status = ReviewTaskStatus.RUNNING
            task.attempts += 1
            try:
                await execute(task)
            except asyncio.CancelledError:
                task.status = ReviewTaskStatus.CANCELLED
                raise
            except Exception as exc:
                task.status = ReviewTaskStatus.FAILED
                task.reason = f"{type(exc).__name__}: {exc}"
            else:
                if task.status == ReviewTaskStatus.DEFERRED_BUDGET:
                    continue
                task.status = ReviewTaskStatus.COMPLETED
                task.reason = None
            completed.append(task)
            self.remove(task.task_id)
        return completed


class ReviewRuntime:
    """运行级队列外壳；预算实现通过 revision 提供资源变化信号。"""

    def __init__(self) -> None:
        self.ready: list[ReviewTask] = []
        self.deferred = DeferredTaskQueue()

    def enqueue(self, task: ReviewTask) -> None:
        task.status = ReviewTaskStatus.READY
        self.ready.append(task)
        self.ready.sort(key=lambda item: (item.priority, item.task_id))

    def defer(self, task: ReviewTask, *, reason: str, budget_revision: int,
              obligation_revision: int = 0) -> None:
        self.deferred.defer(task, reason=reason, budget_revision=budget_revision,
                            obligation_revision=obligation_revision)


class TaskReviewRuntime:
    """唯一工作队列；Unit 公平轮转，每个任务一次只推进一个动作。"""

    engine_version = "task_review_v1"

    def __init__(self, work_items: list[Any] | None = None, *, concurrency: int = 1) -> None:
        if not 1 <= concurrency <= 32:
            raise ValueError("invalid runtime concurrency")
        self.concurrency = concurrency
        self._items: dict[str, Any] = {}
        self._running: set[str] = set()
        self._last_unit: str | None = None
        self._class_cursor: dict[str, int] = {}
        self._served: dict[str, int] = {}
        self._turn = 0
        self.cancelled = False
        self.obligation_revision = 0
        self.checkpoint = None
        self._round_lock = asyncio.Lock()
        for item in work_items or []:
            self.enqueue(item)

    def enqueue(self, item: Any) -> None:
        item_id = getattr(item, "work_item_id", None)
        if not item_id or item_id in self._items:
            raise ValueError("duplicate or missing task_review_v1 work item")
        self._items[item_id] = item

    def ready(self) -> list[Any]:
        return sorted((item for item in self._items.values()
                       if getattr(item, "state", None).value == "ready"),
                       key=lambda item: (item.sequence, item.work_item_id))

    @staticmethod
    def _unit(item: Any) -> str:
        return item.unit_id or item.work_item_id

    def claim_next(self) -> Any | None:
        if self.cancelled:
            return None
        if len(self._running) >= self.concurrency:
            raise RuntimeError("task_review_v1 single writer capacity already running")
        items = self.ready()
        if not items:
            return None
        units = list(dict.fromkeys(self._unit(item) for item in items))
        if self._last_unit in units:
            offset = units.index(self._last_unit) + 1
            units = units[offset:] + units[:offset]
        unit = units[0]
        group = [item for item in items if self._unit(item) == unit]
        # 新调查不能抢过同组尚未执行过的基础检查；其他三类仍有界轮转。
        if any(item.scheduling_class == "basic" and not item.started for item in group):
            group = [item for item in group if item.scheduling_class != "investigation"]
        classes = ("basic", "continuation", "verification", "investigation")
        cursor = self._class_cursor.get(unit, 0)
        for offset in range(len(classes)):
            position = (cursor + offset) % len(classes)
            eligible = [item for item in group if item.scheduling_class == classes[position]]
            if eligible:
                item = min(eligible, key=lambda value: (
                    self._served.get(value.work_item_id, -1), value.sequence, value.work_item_id))
                self._class_cursor[unit] = (position + 1) % len(classes)
                break
        self._last_unit = unit
        self._turn += 1
        self._served[item.work_item_id] = self._turn
        item.state = type(item.state)("running")
        item.attempt_count += 1
        item.started = True
        self._running.add(item.work_item_id)
        return item

    def finish(self, work_item_id: str, *, state: str = "completed") -> Any:
        if work_item_id not in self._running:
            raise RuntimeError("work item is not owned by runtime writer")
        item = self._items[work_item_id]
        item.state = type(item.state)(state)
        item.revision += 1
        self._running.remove(work_item_id)
        return item

    def snapshot(self) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
                for item in sorted(self._items.values(), key=lambda value: value.work_item_id)]

    async def advance_one(self, steps: dict[str, Any]) -> dict[str, Any] | None:
        """领取后持久化，工具/模型推进后保存结果，再更新队列。"""
        item = self.claim_next()
        if item is None:
            return None
        step = steps.get(item.work_item_id)
        if step is None or step.executor.work_item is not item:
            self.finish(item.work_item_id, state="failed")
            raise ValueError("work_item_executor_binding_mismatch")
        step.runtime = self
        try:
            if self.checkpoint is not None:
                await self.checkpoint.persist()
            observation = await step.advance()
        except asyncio.CancelledError:
            self.finish(item.work_item_id, state="cancelled")
            # lease lost 不允许再尝试写回；合法恢复者负责在途请求对账。
            raise
        except Exception as exc:
            rejection = getattr(exc, "admission_rejection", {})
            if rejection.get("kind") == "budget" and rejection.get("can_defer"):
                item.last_checked_budget_revision = step.budget.revision
                item.last_checked_obligation_revision = self.obligation_revision
                item.reason = rejection.get("reason", "budget_rejected")
                self.finish(item.work_item_id, state="deferred_budget")
                if self.checkpoint is not None:
                    await self.checkpoint.persist()
                return {"action": "deferred", "reason": item.reason}
            attempts = [record for record in step.budget.attempts.values()
                        if record.request_id == step.executor.request_id]
            state = "unknown" if any(record.sent and not record.usage_available for record in attempts) else "failed"
            self.finish(item.work_item_id, state=state)
            item.reason = state
            if self.checkpoint is not None and not self.checkpoint.invalidated:
                await self.checkpoint.persist()
            raise
        pending = bool(step.executor.projection.pending_target_ids)
        if self.cancelled:
            state = "cancelled"
        elif (step.pending_read or step.repair_errors or observation.get("action") == "tool"
              or observation.get("page_advanced")):
            state = "ready"
        elif any(error.get("reason") == "repeated_read_no_progress" for error in observation.get("errors", [])):
            state = "waiting_evidence"
            item.reason = "repeated_read_no_progress"
        elif observation.get("errors") or pending:
            state = "failed"
        else:
            state = "completed"
        if state == "ready":
            item.scheduling_class = "continuation"
        self.finish(item.work_item_id, state=state)
        if self.checkpoint is not None:
            await self.checkpoint.persist()
        return observation

    def wake_deferred(self, budget_revision: int) -> int:
        if self.cancelled:
            return 0
        count = 0
        for item in self._items.values():
            if (item.state.value == "deferred_budget" and
                    (item.last_checked_budget_revision != budget_revision or
                     item.last_checked_obligation_revision != self.obligation_revision)):
                item.state = type(item.state)("ready")
                count += 1
        return count

    async def advance_round(self, steps: dict[str, Any]) -> list[Any]:
        """有界并发一轮；没有 Unit/Verifier 的私有 drain。"""
        if self._round_lock.locked():
            return []
        async with self._round_lock:
            return await self._advance_round(steps)

    async def _advance_round(self, steps: dict[str, Any]) -> list[Any]:
        if self.cancelled:
            return []
        budgets = {id(step.budget): step.budget for step in steps.values()}
        if len(budgets) != 1:
            raise ValueError("task runtime requires one shared run budget")
        budget = next(iter(budgets.values()))
        self.wake_deferred(budget.revision)
        count = min(len(self.ready()), self.concurrency - len(self._running))
        results = await asyncio.gather(*(self.advance_one(steps) for _ in range(count)), return_exceptions=True)
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
        return [result for result in results if result is not None]

    async def run(self, steps: dict[str, Any], *, deadline_seconds: float) -> str:
        """运行期限作用于唯一队列，不复制旧 Unit 生命周期。"""
        if deadline_seconds <= 0:
            raise ValueError("runtime deadline must be positive")
        try:
            async with asyncio.timeout(deadline_seconds):
                while not self.cancelled:
                    results = await self.advance_round(steps)
                    if results:
                        continue
                    budget = next(iter(steps.values())).budget
                    released = False
                    # 只释放已经失去合法后续动作的保护，不抹掉未知请求预留。
                    for obligation in list(budget.obligations.values()):
                        owner = self._items.get(obligation.owner_task_id)
                        if (obligation.state.value == "active" and owner is not None
                                and owner.state.value in {"completed", "failed", "cancelled"}):
                            await budget.release_obligation(obligation.obligation_id, abandoned=True)
                            released = True
                    if released and self.wake_deferred(budget.revision):
                        continue
                    states = {item.state.value for item in self._items.values()}
                    if "deferred_budget" in states:
                        return "budget_exhausted"
                    if states <= {"completed"}:
                        return "plan_exhausted"
                    if states & {"unknown", "result_missing", "failed"}:
                        return "technical_failure"
                    return "no_progress"
                return "cancelled"
        except TimeoutError:
            await self.cancel(steps)
            return "deadline"

    async def cancel(self, steps: dict[str, Any]) -> None:
        self.cancelled = True
        for item in self._items.values():
            if item.work_item_id not in self._running and item.state.value not in {
                    "completed", "unknown", "result_missing"}:
                item.state = type(item.state)("cancelled")
        budgets = {id(step.budget): step.budget for step in steps.values()}
        for budget in budgets.values():
            for obligation in list(budget.obligations.values()):
                if obligation.state.value == "active":
                    await budget.release_obligation(obligation.obligation_id, abandoned=True)
        if self.checkpoint is not None:
            await self.checkpoint.persist()

    async def retry_unit(self, unit_id: str, steps: dict[str, Any]) -> list[str]:
        if self.cancelled or self._running:
            raise RuntimeError("retry requires an inactive, uncancelled runtime")
        retried = []
        for item in self._items.values():
            if self._unit(item) != unit_id or item.state.value == "unknown":
                continue
            projection = steps[item.work_item_id].executor.projection
            blocked = {key for key, result in projection.examinations.items()
                       if result.disposition.value == "blocked"}
            gaps = projection.pending_target_ids | blocked
            if not gaps:
                continue
            for key in blocked:
                del projection.examinations[key]
                projection.accepted_target_ids.discard(key)
            projection.pending_target_ids.update(gaps)
            executor = steps[item.work_item_id].executor
            if executor.pages:
                executor.page_index = next((index for index, page in enumerate(executor.pages)
                    if any(value["goal_id"] in gaps and page.page_id not in
                           executor.page_examinations.get(value["goal_id"], {}) for value in page.ranges)),
                    executor.page_index)
                executor.active_evidence_ids = set(executor.pages[executor.page_index].evidence_ids)
            item.state = type(item.state)("ready")
            item.reason = None
            item.scheduling_class = "continuation"
            retried.append(item.work_item_id)
        if self.checkpoint is not None:
            await self.checkpoint.persist()
        return retried
