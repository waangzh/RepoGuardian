"""User Runner 注册、领取和结果上传协议模型。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.models.review import PatchValidationResult, ValidationCheck


class RunnerRegistration(BaseModel):
    """可公开读取的 Runner 元数据；不包含 API Token 或 HMAC 密钥。"""

    model_config = ConfigDict(extra="forbid")

    runner_id: str
    display_name: str
    public_key: str
    allowed_repositories: list[str]
    allowed_profiles: list[str]
    enabled: bool


class RunnerRegistrationRequest(BaseModel):
    """受管理端令牌保护的注册请求；密钥只从 Runner 发往服务端。"""

    model_config = ConfigDict(extra="forbid")

    runner_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=200)
    allowed_repositories: list[str] = Field(min_length=1, max_length=100)
    allowed_profiles: list[str] = Field(min_length=1, max_length=20)
    enabled: bool = True
    api_token: SecretStr
    hmac_secret: SecretStr

    @field_validator("allowed_repositories", "allowed_profiles")
    @classmethod
    def normalize_allow_lists(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not cleaned:
            raise ValueError("allow list cannot be empty")
        return cleaned


class ValidationRequestStatus(str, Enum):
    pending = "pending"
    claimed = "claimed"
    completed = "completed"
    cancelled = "cancelled"
    expired = "expired"


class RepositoryCheckoutInfo(BaseModel):
    """Runner 获取仓库所需的最小信息；不携带 RepoGuardian 凭据。"""

    model_config = ConfigDict(extra="forbid")

    repository_id: str
    clone_url: str
    fetch_ref: str | None = None


class ValidationClaim(BaseModel):
    """成功领取后返回给 Runner 的完整且最小化输入。"""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    repository: RepositoryCheckoutInfo
    base_sha: str
    head_sha: str
    patch_content: str
    validation_profile_id: str
    expires_at: datetime


class RunnerResultSubmission(BaseModel):
    """Runner 签名上传的结构化验证结果。"""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    runner_id: str
    head_sha: str
    patch_sha: str
    profile: str
    checks: list[ValidationCheck] = Field(default_factory=list, max_length=100)
    exit_status: int
    duration_ms: int = Field(ge=0, le=86_400_000)
    environment_fingerprint: str = Field(min_length=1, max_length=1000)
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    idempotency_key: str = Field(min_length=8, max_length=200)
    signature: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")
    log_summary: str = Field(default="", max_length=20_000)
    artifact_references: list[str] = Field(default_factory=list, max_length=20)

    def canonical_payload(self) -> bytes:
        payload = self.model_dump(mode="json", exclude={"signature"})
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


class RunnerResultReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    result: PatchValidationResult
    trusted: Literal[True] = True
    idempotent_replay: bool = False


class ValidationRequestSummary(BaseModel):
    """供 RepoGuardian UI/运维读取的状态，不暴露 patch 或 Runner 密钥。"""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    task_id: str
    patch_id: str
    repository_id: str
    profile: str
    status: ValidationRequestStatus
    runner_id: str | None = None
    expires_at: datetime
    claim_expires_at: datetime | None = None
