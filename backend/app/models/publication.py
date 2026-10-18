"""只读审查结论协议；内容合格不代表已向远端发布。"""

from enum import StrEnum
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.review_plan import FindingView


class ReviewOutcomeKind(StrEnum):
    CLEAN = "clean"
    FINDINGS = "findings"
    INCONCLUSIVE = "inconclusive"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CoverageKind(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class OutcomeReason(StrEnum):
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"
    TERMINATION_INCOMPLETE = "termination_incomplete"
    BUDGET_EXHAUSTED = "budget_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    SCOPE_INCOMPLETE = "scope_incomplete"
    SCOPE_EMPTY = "scope_empty"
    VERIFICATION_UNRESOLVED = "verification_unresolved"
    FINDING_STATUS_UNKNOWN = "finding_status_unknown"
    INVESTIGATION_INCOMPLETE = "investigation_incomplete"
    UNSCHEDULED_INVESTIGATION = "unscheduled_investigation"
    USAGE_INTEGRITY_UNKNOWN = "usage_integrity_unknown"
    SNAPSHOT_MISSING = "snapshot_missing"
    RUNTIME_REPORT_MISSING = "runtime_report_missing"
    RUNTIME_REPORT_INVALID = "runtime_report_invalid"
    UNSUPPORTED_LEGACY_PROJECTION = "unsupported_legacy_projection"


class ReviewOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ReviewOutcomeKind
    coverage: CoverageKind
    confirmed_count: int = Field(default=0, ge=0)
    publishable_count: int = Field(default=0, ge=0)
    unresolved_count: int = Field(default=0, ge=0)
    examined_goals: int = Field(default=0, ge=0)
    total_goals: int = Field(default=0, ge=0)
    incomplete_investigations: int = Field(default=0, ge=0)
    reason_codes: list[OutcomeReason] = Field(default_factory=list)
    finding_conclusion: Literal["confirmed", "unresolved", "no_confirmed_findings", "unknown"] = "unknown"
    review_completeness: CoverageKind = CoverageKind.UNKNOWN
    runtime_reliability: Literal["reliable", "unreliable", "unknown"] = "unknown"
    dynamic_validation: Literal["not_executed", "passed", "failed", "unknown"] = "unknown"


class DigestGoal(BaseModel):
    goal_id: str | None = None
    path: str | None = None
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    side: Literal["head", "base"] | None = None
    status: str = "unknown"
    reason: str | None = None


class DigestExcludedFile(BaseModel):
    path: str
    reason: str


class ReviewDigest(BaseModel):
    pr_url: str | None = None
    author_goal: str | None = None
    actual_changes: list[str] = Field(default_factory=list)
    intent_alignment: str = "未保存独立的目标符合性核验记录，不能由覆盖率推断作者目标已经实现。"
    findings: list[FindingView] = Field(default_factory=list)
    examined_goals: list[DigestGoal] = Field(default_factory=list)
    incomplete_goals: list[DigestGoal] = Field(default_factory=list)
    reviewed_files: list[str] = Field(default_factory=list)
    excluded_files: list[DigestExcludedFile] = Field(default_factory=list)
    validation_state: Literal["not_executed", "unknown"] = "unknown"
    checked_areas: list[Literal["head_source_ranges", "base_source_ranges", "file_changes"]] = Field(default_factory=list)
    termination_reason: str | None = None


class PublicationSnapshot(BaseModel):
    head_sha: str | None = None
    effective_base_sha: str | None = None


class PublicationAnchor(BaseModel):
    file_path: str
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    side: Literal["head", "base"] | None = None
    hunk_id: str | None = None


class PublicationFinding(BaseModel):
    finding_key: str
    candidate_id: str
    severity: Literal["critical", "high", "medium", "low"]
    placement: Literal["inline", "summary"]
    anchor: PublicationAnchor
    title: str
    body_markdown: str
    category: str | None = None
    evidence_summary: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class WithheldFinding(BaseModel):
    candidate_id: str
    reason_code: str


class PublicationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["review-publication-v1"] = "review-publication-v1"
    review_id: str
    snapshot: PublicationSnapshot
    outcome: ReviewOutcome
    digest: ReviewDigest = Field(default_factory=ReviewDigest)
    summary_markdown: str = ""
    user_report_markdown: str = ""
    review_comment_markdown: str = ""
    inline_drafts: list[PublicationFinding] = Field(default_factory=list)
    summary_findings: list[PublicationFinding] = Field(default_factory=list)
    withheld_findings: list[WithheldFinding] = Field(default_factory=list)
    content_eligible: bool = False
    blocking_reasons: list[str] = Field(default_factory=list)


class ReviewCommentCheck(BaseModel):
    review_id: str
    status: Literal["current", "stale", "unknown"]
    reviewed_head_sha: str | None
    latest_head_sha: str | None = None
    checked_at: datetime
    review_comment_markdown: str


class PublicationState(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    PUBLISHING = "publishing"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    STALE = "stale"


class PublicationSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_head_sha: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    publish_summary: Literal[True] = True
    finding_keys: list[str] = Field(default_factory=list, max_length=100)
    acknowledge_partial_review: bool = False


class PublicationItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    publication_id: str
    kind: Literal["summary", "inline"]
    finding_key: str
    fingerprint: str
    payload_hash: str
    state: Literal["pending", "publishing", "published", "failed", "uncertain", "summary_only"]
    github_comment_id: int | None = None
    github_review_id: int | None = None
    github_url: str | None = None
    last_error_code: str | None = None


class PublicationAttempt(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    review_id: str
    repository_id: int
    pr_number: int
    head_sha: str
    plan_hash: str
    selection_hash: str
    publisher_identity: str
    state: PublicationState
    requested_at: datetime
    completed_at: datetime | None = None
    last_error_code: str | None = None
    items: list[PublicationItem] = Field(default_factory=list)


class GitHubSummarySlot(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    repository_id: int
    pr_number: int
    publisher_identity: str
    github_comment_id: int | None = None
    github_url: str | None = None
    latest_review_id: str | None = None
    latest_head_sha: str | None = None
    latest_order: str = ""
    last_payload_hash: str | None = None
    pending_publication_id: str | None = None
    revision: int = 0


class PublicationPreflightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    finding_keys: list[str] | None = Field(default=None, max_length=100)


class GitHubInlineLocation(BaseModel):
    path: str
    line: int = Field(ge=1)
    side: Literal["LEFT", "RIGHT"]


class PublicationInlineCounts(BaseModel):
    selected: int = Field(ge=0)
    ready: int = Field(ge=0)
    already_published: int = Field(ge=0)
    summary_fallback: int = Field(ge=0)
    blocked: int = Field(ge=0)


class PublicationInlinePreview(BaseModel):
    finding_key: str
    action: Literal["create", "already_published", "summary_fallback"]
    body_markdown: str
    location: GitHubInlineLocation | None = None


class PublicationPreflightResult(BaseModel):
    ready: bool
    review_id: str
    plan_hash: str
    head_sha: str
    head_sha_matches: bool
    pr_open: bool
    permissions_ok: bool
    summary_action: Literal["create", "update"]
    summary_comment_id: int | None = None
    finding_keys: list[str]
    inline: PublicationInlineCounts
    inline_previews: list[PublicationInlinePreview]
    summary_markdown: str
    requires_partial_acknowledgement: bool
    warnings: list[str]


class PublicationAccepted(BaseModel):
    publication_id: str
    state: PublicationState
    status_url: str
