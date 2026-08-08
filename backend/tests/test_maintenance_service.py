from pathlib import Path

import pytest

from app.services import maintenance_service as maintenance_module
from app.services.maintenance_service import MaintenanceService
from app.tools.workspace_cleanup import WorkspaceReapResult


class FakeRepository:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def expire_human_requests(self) -> int:
        return 1

    def list_checkpoint_gc_thread_ids(self, thread_ids: list[str]) -> list[str]:
        assert thread_ids == ["terminal-task", "running-task"]
        return ["terminal-task"]

    def list_expired_task_ids(self) -> list[str]:
        return ["expired-task"]

    def delete_task_if_retention_elapsed(self, task_id: str) -> bool:
        self.deleted.append(task_id)
        return True


@pytest.mark.asyncio
async def test_maintenance_coordinates_gc_retention_and_workspace_reaping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = FakeRepository()
    deleted_threads: list[str] = []
    reaper_arguments: dict[str, object] = {}

    async def delete_thread(thread_id: str) -> None:
        deleted_threads.append(thread_id)

    async def list_threads() -> list[str]:
        return ["terminal-task", "running-task"]

    async def compact(**_kwargs: object) -> bool:
        return True

    def reap(**kwargs: object) -> WorkspaceReapResult:
        reaper_arguments.update(kwargs)
        return WorkspaceReapResult(scanned=1, removed=1)

    monkeypatch.setattr(maintenance_module, "delete_thread_checkpoints", delete_thread)
    monkeypatch.setattr(maintenance_module, "list_checkpoint_thread_ids", list_threads)
    monkeypatch.setattr(maintenance_module, "compact_checkpoints_if_needed", compact)
    monkeypatch.setattr(maintenance_module, "reap_orphaned_workspaces", reap)
    monkeypatch.setattr(
        maintenance_module.settings, "repoguardian_workdir", tmp_path / "workspaces"
    )

    service = MaintenanceService(
        repository,  # type: ignore[arg-type]
        active_workspace_paths=lambda: [tmp_path / "active"],
    )
    await service.run_once()

    assert deleted_threads == ["terminal-task"]
    assert repository.deleted == ["expired-task"]
    assert reaper_arguments["active_paths"] == [tmp_path / "active"]
