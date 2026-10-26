"""只读运维能力视图；不包含密钥值或可执行配置。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProviderModelInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    owned_by: str | None = None


class ModelCatalogResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    default_model: str
    models: list[ProviderModelInfo]


class SystemDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    provider: str
    default_model: str
    model_base_url: str
    database_schema_current: bool
    worker_status: Literal["running", "idle"]
    artifact_directory_writable: bool
    langsmith_enabled: bool
    retention_days: int
    configured_secrets: dict[str, bool]


class SystemSettingsUpdate(BaseModel):
    """可由本地设置页修改的非秘密配置。"""

    model_config = ConfigDict(extra="forbid")

    retention_days: int | None = Field(default=None, ge=1, le=3_650)
    model_base_url: str | None = Field(default=None, min_length=1, max_length=2_000)
    model_name: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("model_base_url")
    @classmethod
    def validate_model_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        from urllib.parse import urlsplit

        parsed = urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("model_base_url must be an HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("model_base_url cannot contain credentials, query or fragment")
        return value.strip().rstrip("/")


class PublicationCapability(BaseModel):
    """只读发布能力：仅暴露是否启用，不包含凭据、仓库或操作口令。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    mode: Literal["publish", "copy_only"]


class WorkspaceCleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["expired_only"] = "expired_only"
    confirmed: bool = False


class WorkspaceCleanupPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["expired_only"] = "expired_only"
    ttl_seconds: int
    scanned: int
    eligible: int
    eligible_bytes: int
    failed: int
    skipped_active: int
    skipped_recent: int


class WorkspaceCleanupResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["expired_only"] = "expired_only"
    ttl_seconds: int
    scanned: int
    removed: int
    reclaimed_bytes: int
    failed: int
    skipped_active: int
    skipped_recent: int
