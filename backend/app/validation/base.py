"""统一验证后端协议。"""

from typing import Protocol, runtime_checkable

from app.models.review import (
    PatchValidationRequest,
    PatchValidationResult,
    RepositorySnapshot,
    ValidationCapabilities,
)


@runtime_checkable
class ValidationBackend(Protocol):
    name: str

    async def capabilities(
        self,
        repository: RepositorySnapshot,
    ) -> ValidationCapabilities: ...

    async def validate(
        self,
        request: PatchValidationRequest,
    ) -> PatchValidationResult: ...
