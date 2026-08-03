"""验证后端与 Profile 的服务端策略发现 API。"""

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from app.models.operations import ValidationBackendInfo, ValidationProfilesResponse
from app.models.review import RepositorySnapshot
from app.validation.registry import ValidationBackendRegistry, ValidationBackendPolicyError
from app.validation.user_runner import get_user_runner_service


router = APIRouter(prefix="/validation/backends", tags=["validation-backends"])

_DISPLAY_NAMES = {
    "none": "不执行验证",
    "user_runner": "User Runner",
    "project_ci": "Project CI",
    "gvisor": "gVisor",
}
_SAFETY_BOUNDARIES = {
    "none": "不执行目标仓库代码。",
    "user_runner": "仅向已注册 Runner 发放受租约约束的请求；Profile 必须由服务端预注册。",
    "project_ci": "仅触发仓库显式安装的固定 workflow；不创建 Git ref，不写入仓库内容。",
    "gvisor": "当前为拒绝执行的占位后端；不会回退到宿主机执行。",
}
_DOCUMENTATION = {
    "none": "/docs/agentic-architecture.md",
    "user_runner": "/docs/user-runner-protocol.md",
    "project_ci": "/docs/project-ci-validation.md",
    "gvisor": "/docs/agentic-architecture.md",
}


async def discover_validation_backends() -> list[ValidationBackendInfo]:
    registry = ValidationBackendRegistry()
    repository = RepositorySnapshot(language="unknown", total_files=0)
    checked_at = datetime.now(timezone.utc)
    discovered: list[ValidationBackendInfo] = []
    for name in registry.names:
        backend = registry.get(name)
        try:
            capabilities = await backend.capabilities(repository)
            available = capabilities.available
            reason = capabilities.unavailable_reason
            profiles = capabilities.supported_profiles
            languages = capabilities.supported_languages
            executes_untrusted_code = capabilities.executes_untrusted_code
            requires_configuration = capabilities.requires_user_configuration
        except Exception as exc:
            available = False
            reason = f"capability check failed: {type(exc).__name__}"
            profiles = []
            languages = []
            executes_untrusted_code = name == "project_ci"
            requires_configuration = True

        runner_count = None
        health_status = "healthy" if available else "unavailable"
        if name == "user_runner":
            runner_count = get_user_runner_service().registered_runner_count
            if available and runner_count == 0:
                health_status = "degraded"
                reason = reason or "no enabled User Runner is currently registered"
        discovered.append(ValidationBackendInfo(
            name=name,
            display_name=_DISPLAY_NAMES.get(name, name),
            available=available,
            supported_languages=list(languages),
            supported_profiles=list(profiles),
            executes_untrusted_code=executes_untrusted_code,
            requires_user_configuration=requires_configuration,
            unavailable_reason=reason,
            health_status=health_status,
            last_health_check_at=checked_at,
            safety_boundary=_SAFETY_BOUNDARIES.get(name, "由服务端固定策略控制。"),
            documentation_url=_DOCUMENTATION.get(name, "/docs"),
            registered_runner_count=runner_count,
        ))
    return discovered


@router.get("", response_model=list[ValidationBackendInfo])
async def list_validation_backends() -> list[ValidationBackendInfo]:
    return await discover_validation_backends()


@router.get("/{name}", response_model=ValidationBackendInfo)
async def get_validation_backend(name: str) -> ValidationBackendInfo:
    try:
        ValidationBackendRegistry().get(name)
    except ValidationBackendPolicyError as exc:
        raise HTTPException(status_code=404, detail="Validation backend not found") from exc
    backends = await discover_validation_backends()
    return next(item for item in backends if item.name == name)


@router.get("/{name}/profiles", response_model=ValidationProfilesResponse)
async def get_validation_backend_profiles(name: str) -> ValidationProfilesResponse:
    backend = await get_validation_backend(name)
    return ValidationProfilesResponse(backend=name, profiles=backend.supported_profiles)
