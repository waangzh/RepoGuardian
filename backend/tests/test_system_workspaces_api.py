import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import system as system_api
from app.models.operations import WorkspaceCleanupRequest


@pytest.mark.asyncio
async def test_workspace_cleanup_requires_explicit_confirmation() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await system_api.cleanup_expired_workspaces(
            WorkspaceCleanupRequest(confirmed=False)
        )

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_workspace_preview_and_cleanup_only_remove_expired_inactive_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = tmp_path / "workspaces"
    expired = workdir / "expired"
    recent = workdir / "recent"
    active = workdir / "active"
    for path in (expired, recent, active):
        path.mkdir(parents=True)
        (path / "payload.bin").write_bytes(b"12345")

    os.utime(expired, (100.0, 100.0))
    os.utime(active, (100.0, 100.0))
    monkeypatch.setattr(system_api.settings, "repoguardian_workdir", workdir)
    monkeypatch.setattr(system_api, "workspace_ttl_seconds", lambda: 100)
    monkeypatch.setattr(
        system_api,
        "get_review_service",
        lambda: SimpleNamespace(_repo_paths={"active-task": active}),
    )
    # workspace_cleanup 使用自己的 time 模块，固定时间避免依赖系统时钟。
    from app.tools import workspace_cleanup

    monkeypatch.setattr(workspace_cleanup.time, "time", lambda: 1_000.0)

    preview = await system_api.preview_workspace_cleanup()
    assert preview.scanned == 3
    assert preview.eligible == 1
    assert preview.eligible_bytes == 5
    assert preview.skipped_active == 1
    assert preview.skipped_recent == 1
    assert expired.exists()

    result = await system_api.cleanup_expired_workspaces(
        WorkspaceCleanupRequest(confirmed=True)
    )
    assert result.removed == 1
    assert result.reclaimed_bytes == 5
    assert not expired.exists()
    assert recent.exists()
    assert active.exists()
