"""系统诊断与受限的本地维护 API。"""

import asyncio
import os
from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter, HTTPException, status

from app.api.reviews import get_review_service
from app.api.validation_backends import discover_validation_backends
from app.core.config import settings
from app.core.database import schema_is_current
from app.models.operations import (
    ModelCatalogResponse,
    SystemDiagnostics,
    VersionDiagnostics,
    WorkspaceCleanupPreview,
    WorkspaceCleanupRequest,
    WorkspaceCleanupResponse,
)
from app.services.maintenance_service import workspace_cleanup_lock, workspace_ttl_seconds
from app.services.model_catalog import ModelCatalogError, fetch_model_catalog
from app.tools.workspace_cleanup import inspect_orphaned_workspaces, reap_orphaned_workspaces


router = APIRouter(prefix="/system", tags=["system"])


def _active_workspace_paths() -> tuple[object, ...]:
    service = get_review_service()
    return tuple(service._repo_paths.values())


@router.get("/models", response_model=ModelCatalogResponse)
async def get_available_models() -> ModelCatalogResponse:
    """使用后端配置的凭证查询当前 Provider 实际开放的模型。"""
    try:
        return await fetch_model_catalog(
            provider=settings.repoguardian_provider,
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            default_model=settings.repoguardian_model,
        )
    except ModelCatalogError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@router.get("/diagnostics", response_model=SystemDiagnostics)
async def get_system_diagnostics() -> SystemDiagnostics:
    try:
        app_version = version("repo-guardian-backend")
    except PackageNotFoundError:
        app_version = "0.1.0"

    service = get_review_service()
    worker_running = bool(service._worker_task and not service._worker_task.done())
    artifact_path = settings.repoguardian_artifact_dir.resolve()
    writable_target = artifact_path if artifact_path.exists() else artifact_path.parent

    return SystemDiagnostics(
        version=app_version,
        provider=settings.repoguardian_provider,
        default_model=settings.repoguardian_model,
        database_schema_current=schema_is_current(),
        worker_status="running" if worker_running else "idle",
        artifact_directory_writable=writable_target.exists() and os.access(writable_target, os.W_OK),
        langsmith_enabled=settings.repoguardian_langsmith_tracing,
        security_mode=(
            "unsafe_local"
            if settings.repoguardian_executor == "local"
            and settings.repoguardian_allow_unsafe_local_execution
            else "restricted"
        ),
        patch_max_files=settings.repoguardian_patch_max_files,
        patch_max_changed_lines=settings.repoguardian_patch_max_changed_lines,
        retention_days=settings.repoguardian_retention_days,
        validation_backends=await discover_validation_backends(),
        configured_secrets={
            "github": bool(settings.github_token),
            "llm_provider": bool(settings.openai_api_key),
            "langsmith": bool(settings.langsmith_api_key),
            "runner_admin": bool(settings.repoguardian_runner_registration_token),
            "github_webhook": bool(settings.repoguardian_github_webhook_secret),
        },
        versions=VersionDiagnostics(
            config=settings.repoguardian_config_version,
            prompt=settings.repoguardian_prompt_version,
            rule=settings.repoguardian_rule_version,
            tool_schema=settings.repoguardian_tool_schema_version,
            review_policy=settings.repoguardian_review_policy_version,
            patch_policy=settings.repoguardian_patch_policy_version,
        ),
    )


@router.get("/workspaces/cleanup/preview", response_model=WorkspaceCleanupPreview)
async def preview_workspace_cleanup() -> WorkspaceCleanupPreview:
    """只读扫描达到安全 TTL 的非活动工作目录。"""
    ttl_seconds = workspace_ttl_seconds()
    async with workspace_cleanup_lock:
        result = await asyncio.to_thread(
            inspect_orphaned_workspaces,
            workdir=settings.repoguardian_workdir,
            older_than_seconds=ttl_seconds,
            active_paths=_active_workspace_paths(),
        )
    return WorkspaceCleanupPreview(ttl_seconds=ttl_seconds, **result.__dict__)


@router.post("/workspaces/cleanup", response_model=WorkspaceCleanupResponse)
async def cleanup_expired_workspaces(
    request: WorkspaceCleanupRequest,
) -> WorkspaceCleanupResponse:
    """经显式确认后，清理达到安全 TTL 的非活动工作目录。"""
    if not request.confirmed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Workspace cleanup requires explicit confirmation",
        )

    ttl_seconds = workspace_ttl_seconds()
    async with workspace_cleanup_lock:
        result = await asyncio.to_thread(
            reap_orphaned_workspaces,
            workdir=settings.repoguardian_workdir,
            older_than_seconds=ttl_seconds,
            active_paths=_active_workspace_paths(),
        )
    return WorkspaceCleanupResponse(ttl_seconds=ttl_seconds, **result.__dict__)
