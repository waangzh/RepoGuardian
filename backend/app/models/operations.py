"""只读运维能力视图；不包含密钥值或可执行配置。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ValidationBackendInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str
    available: bool
    supported_languages: list[str] = Field(default_factory=list)
    supported_profiles: list[str] = Field(default_factory=list)
    executes_untrusted_code: bool
    requires_user_configuration: bool
    unavailable_reason: str | None = None
    health_status: Literal["healthy", "degraded", "unavailable"]
    last_health_check_at: datetime
    safety_boundary: str
    documentation_url: str
    registered_runner_count: int | None = None


class ValidationProfilesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: str
    profiles: list[str]


class VersionDiagnostics(BaseModel):
    config: str
    prompt: str
    rule: str
    tool_schema: str
    review_policy: str
    patch_policy: str


class SystemDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    provider: str
    default_model: str
    database_schema_current: bool
    worker_status: Literal["running", "idle"]
    artifact_directory_writable: bool
    langsmith_enabled: bool
    security_mode: Literal["restricted", "unsafe_local"]
    patch_max_files: int
    patch_max_changed_lines: int
    retention_days: int
    validation_backends: list[ValidationBackendInfo]
    configured_secrets: dict[str, bool]
    versions: VersionDiagnostics
