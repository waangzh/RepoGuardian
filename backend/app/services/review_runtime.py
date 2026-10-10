"""确定性审查任务队列和预算暂缓恢复。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Awaitable, Callable


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
            if not bool(getattr(decision, "admitted", decision.get("admitted", False)
                                if isinstance(decision, dict) else False)):
                task.reason = (decision.get("reason") if isinstance(decision, dict)
                               else getattr(decision, "reason", None)) or task.reason
                continue
            task.status = ReviewTaskStatus.RUNNING
            task.attempts += 1
            try:
                await execute(task)
            except Exception as exc:
                task.status = ReviewTaskStatus.FAILED
                task.reason = f"{type(exc).__name__}: {exc}"
            else:
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
