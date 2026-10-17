"""版本化的用户方案；权限和资源上限由服务端归一化。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReviewLimits(PlanModel):
    max_model_calls: int | None = Field(default=None, ge=0, strict=True)
    max_tokens: int | None = Field(default=None, ge=0, strict=True)
    max_cost_microusd: int | None = Field(default=None, ge=0, strict=True)
    timeout_seconds: int | None = Field(default=None, gt=0, strict=True)


class ReviewScope(PlanModel):
    mode: Literal["all", "selected"] = "all"
    primary_files: list[str] = Field(default_factory=list, max_length=5000)

    @field_validator("primary_files")
    @classmethod
    def paths(cls, values):
        from app.models.review import _validate_repo_relative_path
        return sorted(set(_validate_repo_relative_path(value) for value in values))

    @model_validator(mode="after")
    def selection(self):
        if (self.mode == "selected") != bool(self.primary_files):
            raise ValueError("selected scope requires files; all scope must not specify files")
        return self


class ReviewPlanOptions(PlanModel):
    version: Literal["review-plan-v1"] = "review-plan-v1"
    depth: Literal["quick", "standard", "deep"] = "standard"
    focus: list[Literal["correctness", "security", "performance", "compatibility"]] = Field(
        default_factory=lambda: ["correctness", "security", "performance", "compatibility"], min_length=1, max_length=4)
    scope: ReviewScope = Field(default_factory=ReviewScope)
    limits: ReviewLimits = Field(default_factory=ReviewLimits)

    @field_validator("focus")
    @classmethod
    def unique_focus(cls, values):
        return list(dict.fromkeys(values))


class PlanSnapshot(PlanModel):
    head_sha: str
    effective_base_sha: str
    declared_base_sha: str


class ReviewCapabilities(PlanModel):
    schema_version: Literal["review-capabilities-v1"] = "review-capabilities-v1"
    engine: Literal["task_review_v1"] = "task_review_v1"
    plan_version: Literal["review-plan-v1"] = "review-plan-v1"
    policy_version: str
    depth_presets: list[dict]
    focus_categories: list[str]
    scope_selection_supported: bool = True
    context_boundary: str = "repository-tracked-v1; sensitive paths and symlink escapes denied"
    limits: dict
    preferences_sync_supported: bool = False


class FindingDisposition(PlanModel):
    status: Literal["open", "confirmed", "ignored", "resolved"] = "open"
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, value):
        if not value.strip():
            raise ValueError("human disposition requires a reason")
        return value.strip()


class FindingView(PlanModel):
    id: str
    status: str
    severity: str | None = None
    evidence_status: Literal["verified", "insufficient", "unresolved", "unknown"]
    human_confirmation_reason: str | None = None
    dynamic_validation_status: Literal["not_executed", "passed", "failed", "unknown"] = "not_executed"
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
