"""验证后端的服务端注册表。"""

from collections.abc import Iterable

from app.validation.backends import NoValidationBackend, UnavailableValidationBackend
from app.validation.base import ValidationBackend
from app.validation.user_runner import UserRunnerValidationBackend


class ValidationBackendPolicyError(ValueError):
    """API 请求的后端不符合服务端策略。"""


class ValidationBackendRegistry:
    def __init__(self, backends: Iterable[ValidationBackend] | None = None) -> None:
        configured = list(backends) if backends is not None else [
            NoValidationBackend(),
            UserRunnerValidationBackend(),
            UnavailableValidationBackend("project_ci", "project CI is not configured"),
            UnavailableValidationBackend("gvisor", "gVisor validation is not configured"),
        ]
        self._backends = {backend.name: backend for backend in configured}

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._backends)

    def get(self, name: str) -> ValidationBackend:
        try:
            return self._backends[name]
        except KeyError as exc:
            raise ValidationBackendPolicyError(
                f"validation backend '{name}' is not registered by server policy"
            ) from exc
