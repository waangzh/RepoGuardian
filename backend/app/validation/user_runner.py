"""User Runner 验证后端：创建请求后立即返回，不等待 Runner 在线。"""

from datetime import timedelta
from functools import lru_cache

from app.core.config import registered_runner_profiles, settings
from app.models.review import (
    PatchValidationRequest,
    PatchValidationResult,
    RepositorySnapshot,
    ValidationCapabilities,
    ValidationCheck,
    ValidationStatus,
)
from app.services.user_runner_service import UserRunnerService
from app.services.external_validation_repository import ExternalValidationRepository
from app.core.database import schema_is_current


@lru_cache
def get_user_runner_service() -> UserRunnerService:
    return UserRunnerService(
        registered_runner_profiles(),
        claim_timeout=timedelta(
            seconds=settings.repoguardian_runner_claim_timeout_seconds
        ),
        request_timeout=timedelta(
            seconds=settings.repoguardian_runner_request_timeout_seconds
        ),
        redact_values=tuple(filter(None, (
            settings.github_token,
            settings.openai_api_key,
            settings.langsmith_api_key,
            settings.repoguardian_runner_registration_token,
        ))),
        max_log_summary_chars=settings.repoguardian_runner_max_log_summary_chars,
        repository=ExternalValidationRepository() if schema_is_current() else None,
        credential_secret=settings.repoguardian_runner_registration_token,
    )


class UserRunnerValidationBackend:
    name = "user_runner"

    def __init__(self, service: UserRunnerService | None = None) -> None:
        self.service = service or get_user_runner_service()

    async def capabilities(
        self,
        repository: RepositorySnapshot,
    ) -> ValidationCapabilities:
        return ValidationCapabilities(
            available=True,
            supported_languages=[],
            supported_profiles=list(self.service.profile_ids),
            executes_untrusted_code=False,
            requires_user_configuration=True,
        )

    async def validate(
        self,
        request: PatchValidationRequest,
    ) -> PatchValidationResult:
        validation_request = self.service.create_request(request)
        return PatchValidationResult(
            backend=self.name,
            status=ValidationStatus.unsupported,
            head_sha=request.head_sha,
            patch_sha=request.patch_sha,
            checks=[ValidationCheck(
                name="user_runner",
                status=ValidationStatus.unsupported,
                detail="waiting for an authorized User Runner",
            )],
            resolved_failures=[],
            new_failures=[],
            trusted=False,
            trust_source="user_runner",
            validation_request_id=validation_request.request_id,
            profile=validation_request.profile,
        )
