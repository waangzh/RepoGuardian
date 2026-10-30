"""The persisted review boundary; agent proposals carry no execution authority."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.paths import validate_repo_relative_path
from app.models.review.diff import ChangedFile, PullRequestInfo

RunStatus = Literal["queued", "running", "completed", "partial", "failed", "cancelled"]
TERMINAL = frozenset({"completed", "partial", "failed", "cancelled"})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Limits(Model):
    concurrency: int = Field(default=4, ge=1, le=16)
    model_calls: int = Field(default=12, ge=1, le=100)
    tool_calls: int = Field(default=80, ge=1, le=500)
    timeout_seconds: int = Field(default=1800, ge=1, le=86400)
    output_tokens: int = Field(default=4096, ge=256, le=32768)
    total_model_calls: int = Field(default=64, ge=3, le=1000)
    total_input_tokens: int = Field(default=500_000, ge=12000, le=10_000_000)
    total_output_tokens: int = Field(default=262144, ge=1024, le=10_000_000)
    context_tokens: int = Field(default=64000, ge=8192, le=1_000_000)


class Options(Model):
    depth: Literal["quick", "standard", "deep"] = "standard"
    paths: list[str] | None = Field(default=None, max_length=2000)
    focus: list[Literal["correctness", "security", "performance", "compatibility"]] = Field(
        default_factory=lambda: ["correctness", "security", "performance", "compatibility"],
        min_length=1, max_length=4)
    include_pr_discussions: bool = False
    limits: Limits = Field(default_factory=Limits)

    @field_validator("paths")
    @classmethod
    def valid_paths(cls, value):
        return sorted(set(validate_repo_relative_path(path) for path in value)) if value is not None else None


class Configuration(Model):
    provider: str
    endpoint: str
    model: str
    options: Options
    prompt_version: str = "review-vnext-1"


class Snapshot(Model):
    review_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    repository: str
    repository_id: int = Field(gt=0)
    pr_number: int = Field(ge=1)
    declared_base_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    base_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    head_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class SourceRef(Model):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_id: str
    commit_sha: str
    side: Literal["base", "head"]
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def valid_path(cls, value):
        return validate_repo_relative_path(value)


class Location(Model):
    path: str
    line: int = Field(ge=1)
    side: Literal["base", "head"] = "head"

    @field_validator("path")
    @classmethod
    def valid_path(cls, value):
        return validate_repo_relative_path(value)


class Unit(Model):
    id: str
    paths: list[str]
    required_hunk_ids: list[str]
    risk: Literal["low", "medium", "high"]
    focus: list[str]
    estimated_input_tokens: int = Field(ge=1)


class Plan(Model):
    version: str = "hunk-plan-v1"
    units: list[Unit]
    excluded: dict[str, str] = Field(default_factory=dict)
    incremental_from: str | None = None
    history: list[dict] = Field(default_factory=list)


class CandidateInput(Model):
    claim: str = Field(min_length=1, max_length=1500)
    trigger: str = Field(min_length=1, max_length=2000)
    impact: str = Field(min_length=1, max_length=2000)
    source_refs: list[str] = Field(min_length=1, max_length=32)
    location: Location | None = None


class VerificationOutput(Model):
    verdict: Literal["confirmed", "refuted", "insufficient"]
    attribution: Literal["introduced", "exposed", "pre_existing", "uncertain"]
    severity: Literal["critical", "high", "medium", "low"] | None = None
    reason: str = Field(min_length=1, max_length=4000)
    source_refs: list[str] = Field(default_factory=list, max_length=32)
    author_comment: str | None = Field(default=None, max_length=3000)


class UnitCompletion(Model):
    unresolved_reason: str | None = Field(default=None, max_length=1500)


class Finding(Model):
    id: str
    candidate_id: str
    claim: str
    trigger: str
    impact: str
    severity: Literal["critical", "high", "medium", "low"]
    attribution: Literal["introduced", "exposed", "pre_existing", "uncertain"]
    author_comment: str
    location: Location | None
    sources: list[SourceRef]
    publishable: bool
    admission_digest: str


class Candidate(Model):
    id: str
    unit_id: str
    attempt_id: str
    tool_call_id: str
    proposal: CandidateInput
    sources: list[SourceRef]
    verification: VerificationOutput | None = None
    verification_sources: list[SourceRef] = Field(default_factory=list)
    admission_reason: str | None = None
    finding: Finding | None = None


class WorkItem(Model):
    id: str
    kind: Literal["review", "verification"]
    unit_id: str
    candidate_id: str | None = None
    status: Literal["queued", "running", "completed", "interrupted", "failed", "skipped"] = "queued"
    attempt: int = 0
    attempt_id: str | None = None
    delivered_hunk_ids: list[str] = Field(default_factory=list)
    delivered_materials: dict[str, str] = Field(default_factory=dict)
    visible_source_ids: list[str] = Field(default_factory=list)
    missing_material_reasons: dict[str, str] = Field(default_factory=dict)
    material_delivery: Literal["complete", "incomplete", "not_started"] = "not_started"
    error_code: str | None = None
    result: dict | None = None
    tool_calls: int = 0


class OverviewParagraph(Model):
    text: str = Field(min_length=1, max_length=1000)
    source_refs: list[str] = Field(min_length=1, max_length=32)


class OverviewOutput(Model):
    change_overview: list[OverviewParagraph] = Field(min_length=1, max_length=5)
    finding_order: list[str] = Field(default_factory=list, max_length=100)


class Overview(Model):
    status: Literal["pending", "running", "ready", "failed", "skipped_deadline",
                    "skipped_cancelled", "skipped_empty"] = "pending"
    input_hash: str | None = None
    output: OverviewOutput | None = None
    sources: list[SourceRef] = Field(default_factory=list)
    error_code: str | None = None


class Report(Model):
    version: str
    status: RunStatus
    findings: list[Finding]
    candidates: list[Candidate]
    units: list[WorkItem]
    excluded: dict[str, str]
    overview: Overview = Field(default_factory=Overview)
    usage: dict = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)


class Run(Model):
    id: str
    status: RunStatus = "queued"
    phase: str = "prepare"
    pr_url: str
    pr: PullRequestInfo
    snapshot: Snapshot
    configuration: Configuration
    changed_files: list[ChangedFile]
    plan: Plan
    report: Report | None = None
    review_cutoff: datetime
    hard_deadline: datetime
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_code: str | None = None
