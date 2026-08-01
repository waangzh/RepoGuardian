"""由 API 请求与服务端策略共同决定验证后端。"""

from app.models.review import ReviewMode
from app.validation.base import ValidationBackend
from app.validation.registry import ValidationBackendRegistry


class ValidationBackendSelector:
    def __init__(self, registry: ValidationBackendRegistry | None = None) -> None:
        self.registry = registry or ValidationBackendRegistry()

    def select(self, requested_backend: str, mode: ReviewMode | str) -> ValidationBackend:
        selected = "none" if ReviewMode(mode) in {
            ReviewMode.review,
            ReviewMode.review_and_suggest,
        } else requested_backend
        return self.registry.get(selected)
