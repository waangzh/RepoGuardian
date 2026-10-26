"""当前用户方案与只读产品投影；权限和资源上限由服务端归一化。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator


class PlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReviewLimits(PlanModel):
    model_calls: int | None = Field(default=None, ge=1, le=100, strict=True)
    tool_calls: int | None = Field(default=None, ge=1, le=500, strict=True)
    concurrency: int | None = Field(default=None, ge=1, le=16, strict=True)
    timeout_seconds: int | None = Field(default=None, gt=0, strict=True)
    output_tokens: int | None = Field(default=None, ge=256, le=32768, strict=True)


class ReviewScope(PlanModel):
    mode: Literal["all", "selected"] = "all"
    primary_files: list[str] = Field(default_factory=list, max_length=5000)

    @field_validator("primary_files")
    @classmethod
    def paths(cls, values):
        from app.core.paths import validate_repo_relative_path
        return sorted(set(validate_repo_relative_path(value) for value in values))

    @model_validator(mode="after")
    def selection(self):
        if (self.mode == "selected") != bool(self.primary_files):
            raise ValueError("selected scope requires files; all scope must not specify files")
        return self


class ReviewPlanOptions(PlanModel):
    depth: Literal["quick", "standard", "deep"] = "standard"
    focus: list[Literal["correctness", "security", "performance", "compatibility"]] = Field(
        default_factory=lambda: ["correctness", "security", "performance", "compatibility"], min_length=1, max_length=4)
    scope: ReviewScope = Field(default_factory=ReviewScope)
    limits: ReviewLimits = Field(default_factory=ReviewLimits)
    include_pr_discussions: bool | None = Field(default=None, strict=True)

    @model_serializer(mode="wrap")
    def serialize_discussion_choice(self, handler):
        value = handler(self)
        if self.include_pr_discussions is None:
            value.pop("include_pr_discussions", None)
        return value

    @field_validator("focus")
    @classmethod
    def unique_focus(cls, values):
        return list(dict.fromkeys(values))


class PlanSnapshot(PlanModel):
    head_sha: str
    effective_base_sha: str
    declared_base_sha: str


class ReviewCapabilities(PlanModel):
    depth_presets: list[dict]
    focus_categories: list[str]
    scope_selection_supported: bool = True
    context_boundary: str = "fixed Git blobs; sensitive paths and symlink escapes denied"
    limits: dict
    preferences_sync_supported: bool = False
    pr_discussions_available: bool = False
    pr_discussions_default: bool = False


class FindingDisposition(PlanModel):
    status: Literal["open", "confirmed", "ignored", "resolved"] = "open"
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, value):
        if not value.strip():
            raise ValueError("human disposition requires a reason")
        return value.strip()


class FindingLocation(PlanModel):
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    side: Literal["left", "right"]

    @field_validator("path")
    @classmethod
    def normalized_path(cls, value):
        from app.core.paths import validate_repo_relative_path
        return validate_repo_relative_path(value)

    @model_validator(mode="after")
    def ordered_lines(self):
        if self.end_line < self.start_line:
            raise ValueError("end_line precedes start_line")
        return self


class FindingVerification(PlanModel):
    method: Literal["static_model_review"] = "static_model_review"
    evidence_status: Literal["verified", "insufficient", "unresolved", "unknown"] = "unknown"


class FindingView(PlanModel):
    id: str
    attribution: Literal["introduced", "exposed", "pre_existing", "uncertain"] = "uncertain"
    # 机器结论与人工处理、远端投递分开。
    status: str
    review_status: Literal["candidate", "confirmed", "unresolved", "dismissed", "unknown"] = "unknown"
    title: str | None = None
    message: str | None = None
    impact: str | None = None
    recommendation: str | None = None
    category: str | None = None
    location: FindingLocation | None = None
    verification: FindingVerification = Field(default_factory=FindingVerification)
    severity: str | None = None
    evidence_status: Literal["verified", "insufficient", "unresolved", "unknown"]
    human_confirmation_reason: str | None = None
    anchor: dict | None = None
    limitations: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    disposition: FindingDisposition | None = None
    updated_at: datetime | None = None


class FindingsResponse(PlanModel):
    items: list[FindingView]


class FileDiffResponse(PlanModel):
    file_id: int
    file_path: str
    old_file_path: str | None
    snapshot: PlanSnapshot | None
    hunks: list[dict]
    metadata: dict
    next_cursor: int | None
    truncated: bool


class FileContextResponse(PlanModel):
    file_id: int
    file_path: str
    snapshot: PlanSnapshot
    base_lines: list[str]
    head_lines: list[str]
