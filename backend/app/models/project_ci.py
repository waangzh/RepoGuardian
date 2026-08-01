"""Project CI 验证请求、GitHub Actions 运行和结构化结果模型。"""

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.models.review import ValidationCheck


class ProjectCIStatus(str, Enum):
    queued = "queued"
    dispatched = "dispatched"
    running = "running"
    passed = "passed"
    failed = "failed"
    cancelled = "cancelled"
    timed_out = "timed_out"
    inconclusive = "inconclusive"
    infrastructure_error = "infrastructure_error"
    unsupported = "unsupported"


class ProjectCIWorkflow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int | str
    name: str
    path: str
    state: str = "active"


class ProjectCIWorkflowRun(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    repository: str
    workflow_id: int | str | None = None
    workflow_name: str
    ref: str
    status: str
    conclusion: str | None = None
    check_suite_id: int | None = None
    display_title: str | None = None
    html_url: str | None = None


class ProjectCIArtifactResult(BaseModel):
    """由受控 workflow 上传的 result.json；所有绑定字段都是必需的。"""

    model_config = ConfigDict(extra="forbid")

    validation_request_id: str
    repository: str
    workflow_name: str
    ref: str
    run_id: int
    head_sha: str
    patch_sha: str
    profile: str
    checks: list[ValidationCheck] = Field(default_factory=list, max_length=100)
    failure_kind: str | None = None
    log_summary: str | None = Field(default=None, max_length=8_000)
    artifact_references: list[str] = Field(default_factory=list, max_length=20)


class ProjectCIRequestSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    task_id: str
    patch_id: str
    repository: str
    workflow_name: str
    ref: str
    head_sha: str
    patch_sha: str
    profile: str
    status: ProjectCIStatus
    run_id: int | None = None
    check_suite_id: int | None = None
    run_url: str | None = None
    detail: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime


class ProjectCIWebhookReceipt(BaseModel):
    request_id: str
    status: ProjectCIStatus
    accepted: bool = True
    idempotent_replay: bool = False
