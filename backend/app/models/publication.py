"""Publisher 的只读计划与投递协议。"""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PublicationSnapshot(BaseModel):
    declared_base_sha: str | None = None
    effective_base_sha: str | None = None
    head_sha: str | None = None
    repository_id: int | None = None


class PublicationAnchor(BaseModel):
    file_path: str
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    side: Literal["head", "base"] | None = None
    hunk_id: str | None = None


class PublicationFinding(BaseModel):
    finding_key: str
    candidate_id: str
    attribution: Literal["introduced", "exposed"]
    severity: Literal["critical", "high", "medium", "low"]
    placement: Literal["inline", "summary"]
    anchor: PublicationAnchor
    title: str
    body_markdown: str
    affected_behavior: str = ""
    failure_scenario: str = ""
    recommendation: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class WithheldFinding(BaseModel):
    candidate_id: str
    reason_code: str


class ReviewOutcome(BaseModel):
    kind: Literal["clean", "findings", "inconclusive", "failed", "cancelled"]
    coverage: Literal["complete", "partial", "unknown"]
    confirmed_count: int = Field(default=0, ge=0)
    publishable_count: int = Field(default=0, ge=0)
    unresolved_count: int = Field(default=0, ge=0)
    reason_codes: list[str] = Field(default_factory=list)


class ReviewDigest(BaseModel):
    pr_url: str | None = None
    execution_status: str | None = None
    actual_changes: list[str] = Field(default_factory=list)


class PublicationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_id: str
    report_version: str = Field(pattern=r"^[a-f0-9]{64}$")
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
    overview_markdown: str = ""
    overview_status: str = "pending"
    overview_omitted: dict[str, int] = Field(default_factory=dict)
    scope_reasons: list[str] = Field(default_factory=list)
    historical_findings: list[dict] = Field(default_factory=list)


class ReviewSummary(BaseModel):
    review_id: str
    report_version: str
    snapshot: PublicationSnapshot
    outcome: ReviewOutcome
    review_comment_markdown: str = ""
    withheld_findings: list[WithheldFinding] = Field(default_factory=list)
    content_eligible: bool = False
    blocking_reasons: list[str] = Field(default_factory=list)


class ReviewCommentOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    detail: Literal["compact", "full"] = "compact"
    exclude_resolved: bool = False
    finding_key: str | None = Field(default=None, min_length=1, max_length=256)


class ReviewCommentCheck(BaseModel):
    review_id: str
    status: Literal["current", "stale", "unknown"]
    reviewed_head_sha: str | None
    latest_head_sha: str | None = None
    checked_at: datetime
    review_comment_markdown: str
    preview_markdown: str
    included_finding_keys: list[str] = Field(default_factory=list)
    omitted_finding_keys: list[str] = Field(default_factory=list)
    retained_blocking_keys: list[str] = Field(default_factory=list)
    detail: Literal["compact", "full"] = "compact"


class PublicationState(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    PUBLISHING = "publishing"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    STALE = "stale"


class PublicationAccepted(BaseModel):
    publication_id: str
    state: PublicationState
    status_url: str


class PublicationSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_report_version: str = Field(pattern=r"^[a-f0-9]{64}$")
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
    action: Literal["create", "already_published", "summary_fallback", "summary_only"]
    body_markdown: str
    location: GitHubInlineLocation | None = None


class PublicationPreflightResult(BaseModel):
    ready: bool
    review_id: str
    report_version: str
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
