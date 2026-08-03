"""不返回秘密值的只读系统诊断 API。"""

import os
from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter

from app.api.reviews import get_review_service
from app.api.validation_backends import discover_validation_backends
from app.core.config import settings
from app.core.database import schema_is_current
from app.models.operations import SystemDiagnostics, VersionDiagnostics


router = APIRouter(prefix="/system", tags=["system"])


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
