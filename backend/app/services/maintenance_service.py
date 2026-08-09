"""后台维护：人工请求过期、checkpoint GC、任务保留和 workspace 回收。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from pathlib import Path

from app.core.config import settings
from app.graph.checkpointer import (
    compact_checkpoints_if_needed,
    delete_thread_checkpoints,
    list_checkpoint_thread_ids,
)
from app.services.review_repository import ReviewRepository
from app.tools.workspace_cleanup import reap_orphaned_workspaces

logger = logging.getLogger("RepoGuardian.Maintenance")
workspace_cleanup_lock = asyncio.Lock()


def workspace_ttl_seconds() -> int:
    """返回兼顾人工等待窗口的 workspace 最小安全保留时间。"""
    return max(
        settings.repoguardian_orphan_workspace_ttl_seconds,
        settings.repoguardian_human_timeout_seconds
        + settings.repoguardian_maintenance_interval_seconds
        + settings.repoguardian_worker_lease_seconds,
    )


class MaintenanceService:
    def __init__(
        self,
        repository: ReviewRepository,
        *,
        active_workspace_paths: Callable[[], Iterable[Path]] = tuple,
    ) -> None:
        self._repository = repository
        self._active_workspace_paths = active_workspace_paths
        self._stopped = asyncio.Event()

    async def run_once(self) -> None:
        expired_requests = checkpoint_gc_attempts = expired_tasks = 0
        try:
            expired_requests = await asyncio.to_thread(
                self._repository.expire_human_requests
            )
        except Exception:
            logger.warning("人工请求过期维护失败", exc_info=True)

        try:
            existing_thread_ids = await list_checkpoint_thread_ids()
            thread_ids = await asyncio.to_thread(
                self._repository.list_checkpoint_gc_thread_ids,
                existing_thread_ids,
            )
            for thread_id in thread_ids:
                try:
                    await delete_thread_checkpoints(thread_id)
                    checkpoint_gc_attempts += 1
                except Exception:
                    logger.warning(
                        "checkpoint GC 失败: thread_id=%s", thread_id, exc_info=True
                    )
        except Exception:
            logger.warning("读取 checkpoint GC 候选任务失败", exc_info=True)

        try:
            task_ids = await asyncio.to_thread(self._repository.list_expired_task_ids)
            for task_id in task_ids:
                if await asyncio.to_thread(
                    self._repository.delete_task_if_retention_elapsed, task_id
                ):
                    expired_tasks += 1
        except Exception:
            logger.warning("任务保留期维护失败", exc_info=True)

        workspace_ttl = workspace_ttl_seconds()
        try:
            async with workspace_cleanup_lock:
                workspace_result = await asyncio.to_thread(
                    reap_orphaned_workspaces,
                    workdir=settings.repoguardian_workdir,
                    older_than_seconds=workspace_ttl,
                    active_paths=list(self._active_workspace_paths()),
                )
        except Exception:
            workspace_result = None
            logger.warning("孤儿 workspace 回收失败", exc_info=True)

        try:
            compacted = await compact_checkpoints_if_needed(
                min_reclaim_bytes=settings.repoguardian_checkpoint_vacuum_min_bytes,
                min_reclaim_ratio=settings.repoguardian_checkpoint_vacuum_min_ratio,
            )
        except Exception:
            compacted = False
            logger.warning("checkpoint 数据库压缩失败", exc_info=True)

        removed_workspaces = workspace_result.removed if workspace_result else 0
        failed_workspaces = workspace_result.failed if workspace_result else 0
        if any(
            (
                expired_requests,
                checkpoint_gc_attempts,
                expired_tasks,
                removed_workspaces,
                failed_workspaces,
                compacted,
            )
        ):
            logger.info(
                "维护完成: human_expired=%d checkpoint_threads=%d "
                "tasks_expired=%d workspaces_removed=%d workspaces_failed=%d compacted=%s",
                expired_requests,
                checkpoint_gc_attempts,
                expired_tasks,
                removed_workspaces,
                failed_workspaces,
                compacted,
            )

    async def run_forever(self) -> None:
        while not self._stopped.is_set():
            try:
                await asyncio.wait_for(
                    self._stopped.wait(),
                    timeout=settings.repoguardian_maintenance_interval_seconds,
                )
            except TimeoutError:
                await self.run_once()

    def stop(self) -> None:
        self._stopped.set()
