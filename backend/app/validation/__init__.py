"""验证后端公开接口。"""

from app.models.review import (
    PatchValidationRequest,
    PatchValidationResult,
    RepositorySnapshot,
    ValidationCapabilities,
    ValidationCheck,
    ValidationStatus,
)
from app.validation.backends import NoValidationBackend
from app.validation.base import ValidationBackend
from app.validation.registry import ValidationBackendPolicyError, ValidationBackendRegistry
from app.validation.selector import ValidationBackendSelector

__all__ = [
    "NoValidationBackend",
    "PatchValidationRequest",
    "PatchValidationResult",
    "RepositorySnapshot",
    "ValidationBackend",
    "ValidationBackendPolicyError",
    "ValidationBackendRegistry",
    "ValidationBackendSelector",
    "ValidationCapabilities",
    "ValidationCheck",
    "ValidationStatus",
]
