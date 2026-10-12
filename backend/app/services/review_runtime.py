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
    """task_review_v1 的单 writer 运行时。

    P1 只提供确定性的单步 ready/running 投影；预算、lease 和持久化由后续
    阶段接入现有底座。它不消费旧 ReviewTask，也不创建 Unit 私有循环。
    """

    engine_version = "task_review_v1"

    def __init__(self, work_items: list[Any] | None = None) -> None:
        self._items: dict[str, Any] = {}
        self._running: str | None = None
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
                      key=lambda item: item.work_item_id)

    def claim_next(self) -> Any | None:
        if self._running is not None:
            raise RuntimeError("task_review_v1 single writer already running")
        items = self.ready()
        if not items:
            return None
        item = items[0]
        item.state = type(item.state)("running")
        item.attempt_count += 1
        self._running = item.work_item_id
        return item

    def finish(self, work_item_id: str, *, state: str = "completed") -> Any:
        if self._running != work_item_id:
            raise RuntimeError("work item is not owned by runtime writer")
        item = self._items[work_item_id]
        item.state = type(item.state)(state)
        item.revision += 1
        self._running = None
        return item

    def snapshot(self) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
                for item in sorted(self._items.values(), key=lambda value: value.work_item_id)]

    async def advance_one(self, steps: dict[str, Any]) -> dict[str, Any] | None:
        """P1 离线路由：一次领取只推进模型或工具，不 drain 私有循环。"""
        item = self.claim_next()
        if item is None:
            return None
        step = steps.get(item.work_item_id)
        if step is None or step.executor.work_item is not item:
            self.finish(item.work_item_id, state="failed")
            raise ValueError("work_item_executor_binding_mismatch")
        try:
            observation = await step.advance()
        except asyncio.CancelledError:
            self.finish(item.work_item_id, state="cancelled")
            raise
        except Exception:
            self.finish(item.work_item_id, state="failed")
            raise
        pending = bool(step.executor.projection.pending_target_ids)
        if step.pending_read or step.repair_errors or observation.get("action") == "tool":
            state = "ready"
        elif observation.get("errors") or pending:
            state = "failed"
        else:
            state = "completed"
        self.finish(item.work_item_id, state=state)
        return observation
