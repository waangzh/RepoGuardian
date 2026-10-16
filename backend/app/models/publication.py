"""只读审查结论协议；内容合格不代表已向远端发布。"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


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
