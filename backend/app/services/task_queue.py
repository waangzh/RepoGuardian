"""数据库租约任务队列。

claim 使用带资格条件的原子 UPDATE；即使两个 worker 先读到同一候选，也只有一个能更新成功。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable
from uuid import uuid4

from sqlalchemy import or_, select, update

from app.core.config import settings
from app.core.database import sync_session
from app.models.orm import ReviewTaskOrm, WorkerJobOrm, utcnow


@dataclass(frozen=True)
class ClaimedJob:
    id: str
    task_id: str
    unit_id: int | None
    kind: str
    payload: dict[str, Any]
    attempts: int
    lease_owner: str
    lease_until: datetime


class DatabaseTaskQueue:
    def __init__(self, session_factory=sync_session) -> None:
        self._session_factory = session_factory

    def enqueue(
        self,
        *,
        task_id: str,
        kind: str = "review",
        unit_id: int | None = None,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        priority: int = 0,
        available_at: datetime | None = None,
    ) -> str:
        key = idempotency_key or f"{kind}:{task_id}:{unit_id or 'task'}"
        with self._session_factory.begin() as session:
            existing = session.scalar(
                select(WorkerJobOrm).where(WorkerJobOrm.idempotency_key == key)
            )
            if existing:
                if existing.status in {"failed", "dead_letter"}:
                    existing.status = "retry"
                    existing.available_at = available_at or utcnow()
                    existing.last_error = None
                    existing.updated_at = utcnow()
                return existing.id
            row = WorkerJobOrm(
                id=uuid4().hex,
                task_id=task_id,
                unit_id=unit_id,
                kind=kind,
                status="queued",
                idempotency_key=key,
                payload=payload or {},
                priority=priority,
                max_attempts=settings.repoguardian_worker_max_attempts,
                available_at=available_at or utcnow(),
            )
            session.add(row)
            return row.id

    def claim(
        self,
        *,
        worker_id: str,
        lease_seconds: int | None = None,
        kinds: tuple[str, ...] | None = None,
    ) -> ClaimedJob | None:
        now = utcnow()
        lease_until = now + timedelta(
            seconds=lease_seconds or settings.repoguardian_worker_lease_seconds
        )
        for _ in range(8):
            with self._session_factory.begin() as session:
                eligibility = or_(
                    WorkerJobOrm.status.in_(["queued", "retry"]),
                    (WorkerJobOrm.status == "leased") & (WorkerJobOrm.lease_until < now),
                )
                query = (
                    select(WorkerJobOrm.id)
                    .join(ReviewTaskOrm, ReviewTaskOrm.id == WorkerJobOrm.task_id)
                    .where(
                        eligibility,
                        WorkerJobOrm.available_at <= now,
                        WorkerJobOrm.attempts < WorkerJobOrm.max_attempts,
                        ReviewTaskOrm.status != "cancelled",
                        ReviewTaskOrm.deleted_at.is_(None),
                    )
                    .order_by(WorkerJobOrm.priority.desc(), WorkerJobOrm.created_at)
                    .limit(1)
                )
                if kinds:
                    query = query.where(WorkerJobOrm.kind.in_(kinds))
                candidate = session.scalar(query)
                if candidate is None:
                    return None
                claimed = session.execute(
                    update(WorkerJobOrm)
                    .where(WorkerJobOrm.id == candidate, eligibility)
                    .values(
                        status="leased",
                        lease_owner=worker_id,
                        lease_until=lease_until,
                        heartbeat_at=now,
                        attempts=WorkerJobOrm.attempts + 1,
                        updated_at=now,
                    )
                )
                if claimed.rowcount != 1:
                    continue
                row = session.get(WorkerJobOrm, candidate)
                assert row is not None
                return ClaimedJob(
                    id=row.id,
                    task_id=row.task_id,
                    unit_id=row.unit_id,
                    kind=row.kind,
                    payload=dict(row.payload),
                    attempts=row.attempts,
                    lease_owner=worker_id,
                    lease_until=lease_until,
                )
        return None

    def heartbeat(self, job_id: str, worker_id: str, *, lease_seconds: int | None = None) -> bool:
        now = utcnow()
        result = None
        with self._session_factory.begin() as session:
            result = session.execute(
                update(WorkerJobOrm)
                .where(
                    WorkerJobOrm.id == job_id,
                    WorkerJobOrm.status == "leased",
                    WorkerJobOrm.lease_owner == worker_id,
                )
                .values(
                    heartbeat_at=now,
                    lease_until=now + timedelta(
                        seconds=lease_seconds or settings.repoguardian_worker_lease_seconds
                    ),
                    updated_at=now,
                )
            )
        return result.rowcount == 1

    def complete(self, job_id: str, worker_id: str) -> bool:
        now = utcnow()
        with self._session_factory.begin() as session:
            result = session.execute(
                update(WorkerJobOrm)
                .where(
                    WorkerJobOrm.id == job_id,
                    WorkerJobOrm.status == "leased",
                    WorkerJobOrm.lease_owner == worker_id,
                )
                .values(
                    status="completed",
                    completed_at=now,
                    lease_owner=None,
                    lease_until=None,
                    updated_at=now,
                )
            )
            return result.rowcount == 1

    def fail(self, job_id: str, worker_id: str, error: str, *, retry: bool = True) -> str:
        now = utcnow()
        with self._session_factory.begin() as session:
            row = session.get(WorkerJobOrm, job_id)
            if row is None or row.status != "leased" or row.lease_owner != worker_id:
                raise ValueError("worker does not own the job lease")
            can_retry = retry and row.attempts < row.max_attempts
            row.status = "retry" if can_retry else "dead_letter"
            row.available_at = now + timedelta(seconds=min(2 ** row.attempts, 60))
            row.last_error = error[:8000]
            row.lease_owner = None
            row.lease_until = None
            row.updated_at = now
            return row.status

    def cancel_for_task(self, task_id: str) -> int:
        with self._session_factory.begin() as session:
            result = session.execute(
                update(WorkerJobOrm)
                .where(
                    WorkerJobOrm.task_id == task_id,
                    WorkerJobOrm.status.in_(["queued", "retry", "leased"]),
                )
                .values(status="cancelled", lease_owner=None, lease_until=None, updated_at=utcnow())
            )
            return result.rowcount


class ReviewWorker:
    def __init__(
        self,
        queue: DatabaseTaskQueue,
        handler: Callable[[ClaimedJob], Awaitable[None]],
        *,
        worker_id: str | None = None,
    ) -> None:
        self.queue = queue
        self.handler = handler
        self.worker_id = worker_id or f"worker-{uuid4().hex[:12]}"
        self._stopping = asyncio.Event()
        self._active: dict[str, asyncio.Task[None]] = {}

    async def run_once(self) -> bool:
        job = await asyncio.to_thread(self.queue.claim, worker_id=self.worker_id)
        if job is None:
            return False
        heartbeat = asyncio.create_task(self._heartbeat(job.id))
        handler_task = asyncio.create_task(self.handler(job))
        self._active[job.task_id] = handler_task
        try:
            await handler_task
        except asyncio.CancelledError:
            try:
                await asyncio.to_thread(
                    self.queue.fail, job.id, self.worker_id, "task cancelled", retry=True
                )
            except ValueError:
                # cancel_for_task 已原子地把租约改为 cancelled。
                pass
            if asyncio.current_task() and asyncio.current_task().cancelling():
                raise
        except Exception as exc:
            await asyncio.to_thread(
                self.queue.fail, job.id, self.worker_id, str(exc), retry=True
            )
        else:
            await asyncio.to_thread(self.queue.complete, job.id, self.worker_id)
        finally:
            self._active.pop(job.task_id, None)
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        return True

    async def run_forever(self) -> None:
        while not self._stopping.is_set():
            worked = await self.run_once()
            if not worked:
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=settings.repoguardian_worker_poll_seconds
                    )
                except TimeoutError:
                    pass

    def stop(self) -> None:
        self._stopping.set()

    def cancel_task(self, task_id: str) -> bool:
        task = self._active.get(task_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def _heartbeat(self, job_id: str) -> None:
        interval = max(1.0, settings.repoguardian_worker_lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            renewed = await asyncio.to_thread(self.queue.heartbeat, job_id, self.worker_id)
            if not renewed:
                return
