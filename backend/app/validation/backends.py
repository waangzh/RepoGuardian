"""阶段 5A 内置验证后端。"""

from app.models.review import (
    PatchValidationRequest,
    PatchValidationResult,
    RepositorySnapshot,
    ValidationCapabilities,
    ValidationCheck,
    ValidationStatus,
)


class NoValidationBackend:
    """明确记录用户选择不执行目标代码。"""

    name = "none"

    async def capabilities(
        self,
        repository: RepositorySnapshot,
    ) -> ValidationCapabilities:
        return ValidationCapabilities(
            available=True,
            supported_languages=[],
            supported_profiles=[],
            executes_untrusted_code=False,
            requires_user_configuration=False,
        )

    async def validate(
        self,
        request: PatchValidationRequest,
    ) -> PatchValidationResult:
        return PatchValidationResult(
            backend=self.name,
            status=ValidationStatus.unsupported,
            head_sha=request.head_sha,
            patch_sha=request.patch_sha,
            checks=[],
            resolved_failures=[],
            new_failures=[],
            trusted=True,
        )


class UnavailableValidationBackend:
    """已注册但尚未配置的占位后端，绝不执行仓库代码。"""

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self._reason = reason

    async def capabilities(
        self,
        repository: RepositorySnapshot,
    ) -> ValidationCapabilities:
        return ValidationCapabilities(
            available=False,
            supported_languages=[],
            supported_profiles=[],
            executes_untrusted_code=False,
            requires_user_configuration=True,
            unavailable_reason=self._reason,
        )

    async def validate(
        self,
        request: PatchValidationRequest,
    ) -> PatchValidationResult:
        return PatchValidationResult(
            backend=self.name,
            status=ValidationStatus.unsupported,
            head_sha=request.head_sha,
            patch_sha=request.patch_sha,
            checks=[ValidationCheck(
                name="backend_availability",
                status=ValidationStatus.unsupported,
                detail=self._reason,
            )],
            resolved_failures=[],
            new_failures=[],
            trusted=True,
        )
