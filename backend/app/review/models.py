"""Current review entities; model proposals never grant publication authority."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.paths import validate_repo_relative_path

Severity = Literal["critical", "high", "medium", "low"]
Reviewer = Literal["behavior", "contract", "specialist"]
Decision = Literal["confirmed", "refuted", "insufficient_evidence"]


class ReviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReviewSnapshot(ReviewModel):
    review_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    repository: str
    pr_number: int = Field(ge=1)
    base_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    head_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class ReviewAssignment(ReviewModel):
    id: str
    reviewer: Reviewer
    goal_ids: list[str]
    focus_paths: list[str]
    objective: str

    @field_validator("focus_paths")
    @classmethod
    def valid_paths(cls, paths):
        return [validate_repo_relative_path(path) for path in paths]


class ReviewGoal(ReviewModel):
    id: str
    path: str
    side: Literal["head", "base"]
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    status: Literal["not_examined", "examined", "blocked"] = "not_examined"
    reason: str | None = None


class EvidenceRef(ReviewModel):
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

    @model_validator(mode="after")
    def valid_range(self):
        if self.end_line < self.start_line:
            raise ValueError("invalid evidence range")
        return self


class CandidateProposal(ReviewModel):
    title: str = Field(min_length=1, max_length=240)
    trigger: str = Field(min_length=1, max_length=2000)
    expected_behavior: str = Field(min_length=1, max_length=2000)
    actual_behavior: str = Field(min_length=1, max_length=2000)
    impact: str = Field(min_length=1, max_length=2000)
    recommendation: str = Field(min_length=1, max_length=2000)
    severity: Severity
    affected_path: str
    line: int = Field(ge=1)
    evidence_ids: list[str] = Field(min_length=1, max_length=32)
    attribution: Literal["introduced", "exposed", "pre_existing", "uncertain"]

    @field_validator("affected_path")
    @classmethod
    def valid_path(cls, value):
        return validate_repo_relative_path(value)


class CandidateFinding(CandidateProposal):
    id: str
    assignment_ids: list[str]


class ExaminedGoal(ReviewModel):
    goal_id: str
    evidence_ids: list[str] = Field(min_length=1, max_length=32)


class DiscoveryOutput(ReviewModel):
    findings: list[CandidateProposal] = Field(default_factory=list, max_length=30)
    examined: list[ExaminedGoal] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list, max_length=20)


class VerificationOutput(ReviewModel):
    decision: Decision
    explanation: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=32)
    counterevidence_ids: list[str] = Field(default_factory=list, max_length=32)
    attribution: Literal["introduced", "exposed", "pre_existing", "uncertain"]
    severity: Severity | None = None
    equivalent_candidate_ids: list[str] = Field(default_factory=list, max_length=8)
    equivalence_reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def valid_equivalence(self):
        if self.equivalent_candidate_ids and (
                self.decision != "confirmed" or not self.equivalence_reason or self.severity is None):
            raise ValueError("equivalence_requires_confirmed_reason_and_severity")
        if len(set(self.equivalent_candidate_ids)) != len(self.equivalent_candidate_ids):
            raise ValueError("duplicate_equivalence_member")
        return self


class VerificationResult(VerificationOutput):
    candidate_id: str


class AgentResult(ReviewModel):
    id: str
    status: Literal["completed", "failed"]
    output: dict = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)
    error: str | None = None
    model_calls: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    read_errors: int = Field(default=0, ge=0)


class VerifiedFinding(ReviewModel):
    id: str
    candidate: CandidateFinding
    verification: VerificationResult
    evidence_ids: list[str]
    inline_line: int | None
    related_candidate_ids: list[str] = Field(default_factory=list, max_length=8)


class ReviewLimits(ReviewModel):
    concurrency: int = Field(default=4, ge=1, le=16)
    model_calls: int = Field(default=24, ge=1, le=100)
    tool_calls: int = Field(default=80, ge=1, le=500)
    timeout_seconds: int = Field(default=1800, ge=1, le=86400)
    output_tokens: int = Field(default=4096, ge=256, le=32768)


class ReviewReport(ReviewModel):
    snapshot: ReviewSnapshot
    assignments: list[ReviewAssignment]
    goals: list[ReviewGoal]
    discovery_results: list[AgentResult]
    verification_results: list[AgentResult]
    candidates: list[CandidateFinding]
    decisions: list[VerificationResult]
    findings: list[VerifiedFinding]
    complete: bool
    termination: Literal["completed", "partial", "cancelled", "deadline", "failed"]
