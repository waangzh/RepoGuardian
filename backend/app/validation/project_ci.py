"""ProjectCIBackend：把候选补丁 dispatch 到仓库显式安装的安全 workflow。"""

from datetime import timedelta

from app.core.config import registered_project_ci_profiles, settings
from app.models.review import (
    PatchValidationRequest,
    PatchValidationResult,
    RepositorySnapshot,
    ValidationCapabilities,
)
from app.services.external_validation.project_ci_service import ProjectCIService
from app.services.external_validation.external_validation_repository import ExternalValidationRepository
from app.core.database import schema_is_current
from app.tools.github.github_actions import HttpGitHubActionsClient


def build_project_ci_service(on_result=None) -> ProjectCIService | None:
    """构造 Project CI 服务；结果回调由应用组装层注入，验证层不反向依赖启动入口。"""
    if not settings.github_token or not settings.repoguardian_project_ci_workflow:
        return None
    return ProjectCIService(
        HttpGitHubActionsClient(settings.github_token),
        workflow=settings.repoguardian_project_ci_workflow,
        workflow_name=settings.repoguardian_project_ci_workflow_name,
        ref=settings.repoguardian_project_ci_ref,
        profiles=registered_project_ci_profiles(),
        allow_fork=settings.repoguardian_project_ci_allow_fork,
        timeout=timedelta(seconds=settings.repoguardian_project_ci_timeout_seconds),
        poll_interval=settings.repoguardian_project_ci_poll_interval_seconds,
        max_patch_input_bytes=settings.repoguardian_project_ci_max_patch_input_bytes,
        on_result=on_result,
        repository=ExternalValidationRepository() if schema_is_current() else None,
    )


class ProjectCIBackend:
    name = "project_ci"

    def __init__(self, service: ProjectCIService | None = None) -> None:
        if service is None:
            from app.bootstrap.dependencies import get_project_ci_service
            service = get_project_ci_service()
        self.service = service

    async def capabilities(self, repository: RepositorySnapshot) -> ValidationCapabilities:
        profiles = list(self.service.profiles) if self.service else []
        return ValidationCapabilities(
            available=self.service is not None,
            supported_languages=[],
            supported_profiles=profiles,
            executes_untrusted_code=True,
            requires_user_configuration=True,
            unavailable_reason=(
                None
                if self.service
                else "project CI is not configured"
            ),
        )

    async def validate(self, request: PatchValidationRequest) -> PatchValidationResult:
        if self.service is None:
            raise RuntimeError("project CI is not configured")
        return await self.service.dispatch(request)
