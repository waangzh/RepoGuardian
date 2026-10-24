"""Review Unit 与计划投影模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import (
    BaseModel,
    Field,
    model_validator,
)

from app.models.incremental import IncrementalPreview
from app.models.review_plan import ReviewPlanOptions, PlanSnapshot
from app.models.review.common import ReviewMode, ReviewUnitComplexity, ValidationBackend
from app.models.review.diff import ContextProvenance, ExcludedReviewFile, PlannedChangedFile

class ReviewUnit(BaseModel):
    """由确定性 Planner 生成的最小独立审查单元。"""

    id: str
    primary_files: list[str] = Field(min_length=1)
    related_files: list[str] = Field(default_factory=list)
    context_provenance: list[ContextProvenance] = Field(default_factory=list)
    diff_hunk_ids: list[str] = Field(default_factory=list)
    changed_symbols: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    risk_tags: list[str] = Field(default_factory=list)
    estimated_tokens: int = Field(ge=0)
    complexity: ReviewUnitComplexity
    fingerprint: str
    grouping_reason: str

    @model_validator(mode="after")
    def validate_file_roles(self) -> "ReviewUnit":
        self.primary_files = list(dict.fromkeys(self.primary_files))
        primary = set(self.primary_files)
        self.related_files = [
            path for path in dict.fromkeys(self.related_files) if path not in primary
        ]
        readable = primary | set(self.related_files)
        self.context_provenance = [
            item for item in self.context_provenance if item.file in readable
        ]
        return self


class ReviewToolScope(BaseModel):
    """单个 Unit 的两阶段只读权限：仓库发现可扩张，评论范围不可扩张。"""

    review_unit_id: str
    commentable_files: set[str]
    seed_files: set[str] = Field(default_factory=set)
    readable_files: set[str]
    repository_discovery_enabled: bool = True
    context_provenance: dict[str, ContextProvenance] = Field(default_factory=dict)
    repository_root: str | None = None
    max_lines_per_read: int = Field(gt=0)
    max_search_results: int = Field(gt=0)
    max_context_chars: int = Field(default=60_000, gt=0)

    @model_validator(mode="after")
    def require_commentable_subset(self) -> "ReviewToolScope":
        if not self.commentable_files <= self.readable_files:
            raise ValueError("commentable_files must be a subset of readable_files")
        if not set(self.context_provenance) <= self.readable_files:
            raise ValueError("context provenance files must be readable")
        if not self.seed_files:
            self.seed_files = set(self.commentable_files)
        if not self.seed_files <= self.readable_files:
            raise ValueError("seed_files must be a subset of readable_files")
        return self


class ReviewPlan(BaseModel):
    planner_version: str
    changed_files: list[PlannedChangedFile] = Field(default_factory=list)
    review_units: list[ReviewUnit] = Field(default_factory=list)
    excluded_files: list[ExcludedReviewFile] = Field(default_factory=list)
    matched_rules: list[str] = Field(default_factory=list)
    risk_tags: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ReviewPreviewResponse(BaseModel):
    force_full_review: bool = False
    continuation: dict | None = None
    existing_review_id: str | None = None
    preview_id: str | None = None
    plan_hash: str | None = None
    snapshot: PlanSnapshot | None = None
    effective_plan: ReviewPlanOptions | None = None
    expires_at: datetime | None = None
    estimates: dict = Field(default_factory=lambda: {"scope": "initial_only", "total_usage_prediction": False})
    mode: ReviewMode
    changed_file_count: int = Field(ge=0)
    included_file_count: int = Field(ge=0)
    changed_files: list[PlannedChangedFile] = Field(default_factory=list)
    review_units: list[ReviewUnit] = Field(default_factory=list)
    excluded_files: list[ExcludedReviewFile] = Field(default_factory=list)
    matched_rules: list[str] = Field(default_factory=list)
    risk_tags: list[str] = Field(default_factory=list)
    planning_model_calls: int = Field(ge=0)
    estimated_model_calls: int = Field(ge=0)
    max_model_calls: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
    patch_generation_enabled: bool
    validation_backend: "ValidationBackendPreview"
    incremental: "IncrementalPreview | None" = None
    warnings: list[str] = Field(default_factory=list)


class ValidationBackendPreview(BaseModel):
    name: ValidationBackend
    available: bool
    unavailable_reason: str | None = None
