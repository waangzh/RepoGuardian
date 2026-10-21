"""只读运维能力视图；不包含密钥值或可执行配置。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ProviderModelInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    owned_by: str | None = None


class ModelCatalogResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    default_model: str
    models: list[ProviderModelInfo]


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
    configured_secrets: dict[str, bool]
    versions: VersionDiagnostics


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
