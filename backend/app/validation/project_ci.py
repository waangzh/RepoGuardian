"""ProjectCIBackend：把候选补丁 dispatch 到仓库显式安装的安全 workflow。"""

from datetime import timedelta
from functools import lru_cache

from app.core.config import registered_project_ci_profiles, settings
from app.models.review import (
    PatchValidationRequest,
    PatchValidationResult,
    RepositorySnapshot,
    ValidationCapabilities,
)
from app.services.project_ci_service import ProjectCIService
from app.tools.github_actions import HttpGitHubActionsClient


@lru_cache
def get_project_ci_service() -> ProjectCIService | None:
    if not settings.github_token or not settings.repoguardian_project_ci_workflow:
        return None
    def apply_result(summary, result) -> None:
        from app.api.reviews import get_review_service

        get_review_service().apply_project_ci_result(summary.task_id, summary.patch_id, result)

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
        on_result=apply_result,
    )


class ProjectCIBackend:
    name = "project_ci"

    def __init__(self, service: ProjectCIService | None = None) -> None:
        self.service = service if service is not None else get_project_ci_service()

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
