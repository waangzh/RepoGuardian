"""Current product contracts for plan confirmation, progress and saved reviews."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, HttpUrl

from app.models.review.diff import ChangedFile, PlannedChangedFile, PullRequestInfo
from app.models.review_plan import PlanSnapshot, ReviewPlanOptions
from app.review.models import ReviewAssignment, ReviewGoal, ReviewLimits, ReviewModel, ReviewReport

TERMINAL_STATUSES = frozenset({"completed", "completed_with_warnings", "failed", "cancelled"})


def utcnow():
    return datetime.now(timezone.utc)


class ReviewConfiguration(ReviewModel):
    provider: str
    endpoint: str
    model: str
    options: ReviewPlanOptions
    limits: ReviewLimits


class ReviewPreviewRequest(ReviewModel):
    pr_url: HttpUrl
    model: str | None = Field(default=None, min_length=1, max_length=200)
    plan: ReviewPlanOptions = Field(default_factory=ReviewPlanOptions)


class ReviewCreateRequest(ReviewModel):
    pr_url: HttpUrl
    preview_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    expected_head_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    expected_effective_base_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class ReviewPreviewResponse(ReviewModel):
    preview_id: str
    snapshot: PlanSnapshot
    effective_plan: ReviewPlanOptions
    expires_at: datetime
    changed_files: list[PlannedChangedFile]
    assignments: list[ReviewAssignment]
    goals: list[ReviewGoal]
    limits: ReviewLimits
    warnings: list[str] = Field(default_factory=list)


class ReviewProgress(ReviewModel):
    name: str
    status: Literal["running", "completed", "failed"]
    message: str
    data: dict = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=utcnow)


class ReviewTask(ReviewModel):
    id: str
    status: Literal["queued", "planning", "reviewing", "verifying_issues", "completed",
                    "completed_with_warnings", "failed", "cancelled"] = "queued"
    phase: str = "prepare"
    pr_url: str
    configuration: ReviewConfiguration
    snapshot: PlanSnapshot
    pr: PullRequestInfo
    changed_files: list[ChangedFile]
    review_report: ReviewReport | None = None
    pr_discussion: dict | None = None
    steps: list[ReviewProgress] = Field(default_factory=list)
    report_markdown: str | None = None
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None


class ReviewCreateResponse(ReviewModel):
    task_id: str
    status: str
    snapshot: PlanSnapshot
    confirmed_plan: ReviewPlanOptions


class ReviewTaskListResponse(ReviewModel):
    items: list[ReviewTask]
    total: int
    page: int
    page_size: int
