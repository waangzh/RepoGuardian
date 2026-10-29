"""Current review entities; model proposals never grant publication authority."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.paths import validate_repo_relative_path

Severity = Literal["critical", "high", "medium", "low"]
Reviewer = Literal["reviewer", "contract", "specialist"]
Decision = Literal["confirmed", "refuted", "insufficient_evidence"]


class ReviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReviewSnapshot(ReviewModel):
    review_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    repository: str
    pr_number: int = Field(ge=1)
    base_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    head_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class ReviewUnit(ReviewModel):
    """A logically related change set reviewed as one unit by default."""

    id: str
    changed_paths: list[str]
    changed_hunk_ids: list[str]
    changed_symbols: list[str]
    impacted_paths: list[str]
    risk_tier: Literal["low", "medium", "high"]
    risk_reasons: list[str]
    strategy: Literal["fast_pass", "contextual_review", "deep_investigation"]
    estimated_input_tokens: int = Field(ge=1)
    min_model_calls: int = Field(ge=1)
    max_model_calls: int = Field(ge=1)
    goal_ids: list[str]

    @field_validator("changed_paths")
    @classmethod
    def valid_paths(cls, paths):
        return [validate_repo_relative_path(path) for path in paths]


class ReviewAssignment(ReviewModel):
    id: str
    unit_id: str
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
    kind: Literal["source", "structural"] = "source"
    path: str
    side: Literal["head", "base"]
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    description: str | None = Field(default=None, max_length=500)
    status: Literal["not_examined", "examined", "blocked"] = "not_examined"
    reason: str | None = None

    @model_validator(mode="after")
    def valid_range(self):
        if self.kind == "source":
            if (self.start_line is None or self.end_line is None
                    or self.end_line < self.start_line):
                raise ValueError("invalid goal range")
        elif self.start_line is not None or self.end_line is not None:
            raise ValueError("structural goal has no line range")
        return self


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


class ChangeObservationProposal(ReviewModel):
    change: str = Field(min_length=1, max_length=600,
        description="Neutral source-backed description of what changed. No suspected defects, "
            "test-quality criticism, unverified omissions or recommendations; submit those as candidates.")
    before: str | None = Field(default=None, max_length=400)
    after: str | None = Field(default=None, max_length=400)
    impact: str = Field(min_length=1, max_length=400,
        description="Direct affected behavior, not a defect verdict. This analysis is kept for audit, "
            "not admitted as a verified finding.")
    kind: Literal["implementation", "test_source"]
    evidence_ids: list[str] = Field(min_length=1, max_length=12)


class ChangeObservation(ChangeObservationProposal):
    id: str
    unit_id: str
    assignment_id: str
    sources: list[EvidenceRef]


class DiscoveryOutput(ReviewModel):
    findings: list[CandidateProposal] = Field(default_factory=list, max_length=30)
    observations: list[ChangeObservationProposal] = Field(default_factory=list, max_length=6)
    examined: list[ExaminedGoal] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list, max_length=20,
        description="Proposed missing premises, not accepted blockers. Name the specific missing fact, "
            "which conclusion about this change depends on it, and the smallest source check needed. "
            "Not running code/tests/builds is a general boundary, even for numerical changes: put that in "
            "boundary_notes. Do not require exhaustive unrelated consumers or optional tests.")
    boundary_notes: list[str] = Field(default_factory=list, max_length=10,
        description="General static-review boundaries, including not running code/tests/builds. "
            "Static source reasoning is allowed and does not itself imply missing evidence.")


class LimitationResolution(ReviewModel):
    limitation_id: str
    status: Literal["resolved", "boundary"]
    explanation: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(min_length=1, max_length=32)


class CandidateLimitationResolution(LimitationResolution):
    candidate_id: str


class LimitationAssessment(ReviewModel):
    limitation_id: str
    status: Literal["unresolved", "resolved", "boundary"]
    explanation: str = Field(min_length=1, max_length=2000)
    goal_ids: list[str] = Field(min_length=1, max_length=20)
    evidence_ids: list[str] = Field(min_length=1, max_length=32)
    missing_fact: str | None = Field(default=None, max_length=1000)
    dependent_conclusion: str | None = Field(default=None, max_length=1000)
    change_relevance: str | None = Field(default=None, max_length=1000)
    next_check: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def necessary_gap_requires_dependency(self):
        if self.status == "unresolved" and not all(
                value and value.strip() for value in (
                    self.missing_fact, self.dependent_conclusion, self.change_relevance, self.next_check)):
            raise ValueError("necessary_gap_requires_change_dependency_and_next_check")
        return self


class LimitationReviewOutput(ReviewModel):
    assessments: list[LimitationAssessment] = Field(min_length=1, max_length=3,
        description="Exactly one assessment for each supplied limitation ID, independently of bug candidates. "
            "unresolved is only a necessary evidence gap with a concrete change dependency. "
            "General boundaries and unnecessary universal proofs belong to boundary.")
    findings: list[CandidateProposal] = Field(default_factory=list, max_length=5,
        description="Only concrete bugs discovered during this check; they still require independent verification.")


class ReviewLimitation(ReviewModel):
    id: str
    source: Literal["discovery", "verification"]
    source_id: str
    text: str
    candidate_ids: list[str] = Field(default_factory=list)
    goal_ids: list[str] = Field(default_factory=list)
    status: Literal["pending", "unresolved", "resolved", "boundary", "nonblocking"] = "pending"
    resolution: LimitationResolution | None = None
    assessment: LimitationAssessment | None = None
    resolved_by: str | None = None
    resolution_claims: list[CandidateLimitationResolution] = Field(default_factory=list)


class AuthorComment(ReviewModel):
    trigger: str = Field(min_length=1, max_length=800)
    cause: str = Field(min_length=1, max_length=1000)
    impact: str = Field(min_length=1, max_length=800)
    recommendation: str = Field(min_length=1, max_length=800)
    body: str | None = Field(default=None,
        description="Final Simplified Chinese prose for the author: one or two plain-text paragraphs, "
        "with trigger, causal failure, actual impact and repair. No title, severity, links or evidence log.")

    @field_validator("body", mode="before")
    @classmethod
    def valid_body(cls, value):
        if (not isinstance(value, str) or not value.strip() or len(value) > 3000
                or len(value.strip().split("\n\n")) > 2
                or any(ord(char) < 32 and char not in "\n\t" for char in value)
                or any(mark in value for mark in ("<", ">", "://", "```", "**", "\n#", "\n- "))):
            return None
        return value.strip()


class VerificationOutput(ReviewModel):
    decision: Decision
    explanation: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=32)
    counterevidence_ids: list[str] = Field(default_factory=list, max_length=32)
    attribution: Literal["introduced", "exposed", "pre_existing", "uncertain"]
    severity: Severity | None = None
    author_comment: AuthorComment | None = None
    equivalent_candidate_ids: list[str] = Field(default_factory=list, max_length=8)
    equivalence_reason: str | None = Field(default=None, max_length=2000)
    limitations: list[str] = Field(default_factory=list, max_length=20,
        description="Necessary evidence gaps. MUST be empty for confirmed/refuted; otherwise choose insufficient_evidence.")
    boundary_notes: list[str] = Field(default_factory=list, max_length=10,
        description="General boundaries such as not executing tests; these do not imply missing decision evidence.")
    limitation_resolutions: list[LimitationResolution] = Field(default_factory=list, max_length=20,
        description="Explicit assessments of supplied review_limitations relevant to this candidate. "
            "Use only supplied limitation IDs and independently read source evidence. "
            "resolved means the specific missing premise was established; boundary means an explained "
            "general review boundary, not a necessary missing premise. Omit genuinely unresolved or unrelated gaps. "
            "Refuting a candidate does not automatically resolve any limitation.")

    @field_validator("author_comment", mode="before")
    @classmethod
    def valid_author_comment(cls, value):
        if value is None or isinstance(value, AuthorComment):
            return value
        try:
            return AuthorComment.model_validate(value)
        except (ValueError, TypeError):
            return None

    @model_validator(mode="after")
    def limitations_require_uncertainty(self):
        if self.limitations and self.decision != "insufficient_evidence":
            raise ValueError("limitations_require_insufficient_evidence")
        return self

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


class BatchVerificationOutput(ReviewModel):
    decisions: list[VerificationResult] = Field(min_length=1, max_length=3,
        description="Exactly one decision for each supplied candidate_id; never omit, repeat, or invent an ID.")


class AgentAttempt(ReviewModel):
    status: Literal["completed", "failed"]
    evidence_ids: list[str] = Field(default_factory=list)
    error: str | None = None
    model_calls: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    tool_calls: int | None = None
    tool_failures: int = Field(default=0, ge=0)
    unconsumed_pages: int = Field(default=0, ge=0)
    investigation_limitations: list[str] = Field(default_factory=list, max_length=20)
    policy_exclusions: int = Field(default=0, ge=0)
    incomplete_searches: int = Field(default=0, ge=0)
    structured_output_retries: int = Field(default=0, ge=0)
    cached_input_tokens: int | None = None


class AgentResult(AgentAttempt):
    id: str
    output: dict = Field(default_factory=dict)
    previous_attempts: list[AgentAttempt] = Field(default_factory=list, max_length=1)
    execution_id: str | None = None


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
    total_model_calls: int = Field(default=64, ge=1, le=1000)
    total_input_tokens: int = Field(default=500_000, ge=1000, le=10_000_000)


class SynthesisParagraph(ReviewModel):
    source_ids: list[str] = Field(min_length=1, max_length=8)
    text: str = Field(min_length=1, max_length=3000,
        description="Write a concise natural-language synthesis supported by the referenced facts. "
        "Preserve conditions, negation and uncertainty; do not invent facts or policy conclusions. "
        "Follow the input source_policy for this section. Observations describe changes, not defects.")


class SynthesisOutput(ReviewModel):
    change_overview: list[SynthesisParagraph] = Field(min_length=1, max_length=3)
    review_overview: list[SynthesisParagraph] = Field(min_length=1, max_length=2)
    actions: list[SynthesisParagraph] = Field(min_length=1, max_length=2)
    disclosed_status_ids: list[str] = Field(default_factory=list, max_length=200)


class SynthesisGrounding(ReviewModel):
    supported: bool
    explanation: str = Field(min_length=1, max_length=2000)


class SynthesisAttempt(ReviewModel):
    stage: Literal["generation", "grounding"]
    number: Literal[1, 2]
    request_id: str
    reservation_id: str | None = None
    timeout_seconds: float = Field(gt=0)
    status: Literal["started", "completed", "timeout", "failed", "unknown"]
    retryable: bool = False
    feedback: str | None = None
    error: str | None = None
    result: dict | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None


class ReviewSynthesis(ReviewModel):
    status: Literal["ready", "fallback"] = "fallback"
    material_version: str
    error: str | None = None
    omitted: dict[str, int] = Field(default_factory=dict)
    output: SynthesisOutput | None = None
    model_calls: int = Field(default=0, ge=0)
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    attempts: list[SynthesisAttempt] = Field(default_factory=list, max_length=4)
    grounding: SynthesisGrounding | None = None
    grounded_output_hash: str | None = None
    started_at: float | None = None


class ReviewReport(ReviewModel):
    snapshot: ReviewSnapshot
    review_units: list[ReviewUnit] = Field(default_factory=list)
    assignments: list[ReviewAssignment]
    goals: list[ReviewGoal]
    discovery_results: list[AgentResult]
    verification_results: list[AgentResult]
    limitation_results: list[AgentResult] = Field(default_factory=list)
    candidates: list[CandidateFinding]
    decisions: list[VerificationResult]
    findings: list[VerifiedFinding]
    observations: list[ChangeObservation] = Field(default_factory=list)
    observation_errors: list[str] = Field(default_factory=list)
    synthesis: ReviewSynthesis | None = None
    complete: bool
    termination: Literal["completed", "partial", "cancelled", "deadline", "failed"]
    coverage_limitations: list[str] = Field(default_factory=list)
    budget: dict = Field(default_factory=dict)
