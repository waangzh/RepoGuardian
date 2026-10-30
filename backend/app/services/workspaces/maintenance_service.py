"""Retention and disposable workspace maintenance; no model session history."""

import asyncio
import logging

from app.core.config import settings
from app.services.workspaces.prepared_workspaces import PreparedWorkspaces
from app.tools.workspace_cleanup import reap_orphaned_workspaces

logger = logging.getLogger("RepoGuardian.Maintenance")
workspace_cleanup_lock = asyncio.Lock()


def workspace_ttl_seconds():
    return settings.repoguardian_orphan_workspace_ttl_seconds


class MaintenanceService:
    def __init__(self, repository, *, active_workspace_paths=tuple):
        self.store = repository
        self.active_paths = active_workspace_paths
        self._stopped = asyncio.Event()

    async def run_once(self):
        try:
            for run_id in await asyncio.to_thread(self.store.list_expired_task_ids):
                await asyncio.to_thread(self.store.delete_task, run_id, require_retention=True)
            await asyncio.to_thread(PreparedWorkspaces(self.store).maintain)
            async with workspace_cleanup_lock:
                await asyncio.to_thread(reap_orphaned_workspaces,
                    workdir=settings.repoguardian_workdir, older_than_seconds=workspace_ttl_seconds(),
                    active_paths=list(self.active_paths()))
        except Exception:
            logger.warning("maintenance_failed")

    async def run_forever(self):
        while not self._stopped.is_set():
            try:
                await asyncio.wait_for(self._stopped.wait(),
                                       timeout=settings.repoguardian_maintenance_interval_seconds)
            except TimeoutError:
                await self.run_once()

    def stop(self):
        self._stopped.set()
