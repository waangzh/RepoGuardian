"""task_review_v1 的最小协议和服务端状态模型。

模型只提供当前任务的取证动作或结果提交；目标身份、快照、证据和完成状态
由服务端绑定。此模块不复用旧 UnitReviewRecord/question_updates 协议。
"""

from __future__ import annotations

from enum import StrEnum
import hashlib
import json
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator, model_serializer
from app.models.review import (FileReadRequest, FileFindRequest, CodeSearchRequest,
                              FileReadDiffRequest, IssueCategory, Severity)
from app.models.pr_discussion import DiscussionReadArguments, DiscussionMatch, DiscussionUsage


Revision = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$", min_length=40, max_length=40)]
RelativePath = Annotated[str, StringConstraints(min_length=1, max_length=1_000)]


def _path(value: str) -> str:
    parsed = PurePosixPath(value)
    if (
        "\\" in value or "\x00" in value or parsed.is_absolute()
        or parsed.as_posix() != value or any(part in {"", ".", ".."} for part in parsed.parts)
        or ":" in value
    ):
        raise ValueError("repository path must be a normalized relative POSIX path")
    return value


class ReviewEngine(StrEnum):
    task_review_v1 = "task_review_v1"
    legacy_units = "legacy_units"


class WorkItemKind(StrEnum):
    inspect = "inspect"
    investigate = "investigate"
    verify = "verify"


class WorkItemState(StrEnum):
    ready = "ready"
    running = "running"
    waiting_evidence = "waiting_evidence"
    deferred_budget = "deferred_budget"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    unknown = "unknown"
    result_missing = "result_missing"


class ExaminationDisposition(StrEnum):
    examined = "examined"
    blocked = "blocked"


class InvestigationAnswer(StrEnum):
    supported = "supported"
    refuted = "refuted"
    insufficient_evidence = "insufficient_evidence"


class VerificationDecision(StrEnum):
    supported = "supported"
    refuted = "refuted"
    insufficient_evidence = "insufficient_evidence"


class RuntimeReadArguments(FileReadRequest):
    end_line: int = Field(ge=1, le=1_000_000)

    @model_validator(mode="after")
    def valid_range(self) -> "RuntimeReadArguments":
        self.file_path = _path(self.file_path)
        if self.end_line < self.start_line or self.end_line - self.start_line + 1 > 2_000:
            raise ValueError("read range is invalid or too large")
        return self


class ReviewRunSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    run_id: str = Field(min_length=1, max_length=128)
    engine_version: Literal["task_review_v1"] = "task_review_v1"
    snapshot_head_sha: Revision
    snapshot_base_sha: Revision
    diff_manifest_hash: str = Field(min_length=1, max_length=128)
    model_provider: str = Field(min_length=1, max_length=100)
    model_name: str = Field(min_length=1, max_length=200)
    policy_version: str = Field(min_length=1, max_length=100)
    protocol_version: Literal["task-review-protocol-v1"] = "task-review-protocol-v1"
    prompt_version: Literal["task-review-prompt-v1", "task-review-prompt-v2", "task-review-prompt-v3", "task-review-prompt-v4", "task-review-prompt-v5", "task-review-prompt-v6", "task-review-prompt-v7", "task-review-prompt-v8"] = "task-review-prompt-v1"
    publication_contract: Literal["legacy", "claim-publication-v1"] = "legacy"
    max_tokens: int = Field(ge=0)
    max_calls: int = Field(ge=0)
    readable_scope_files: tuple[RelativePath, ...] = ()
    review_strategy: dict = Field(default_factory=dict)
    pr_intent_context: str | None = Field(default=None, min_length=1, max_length=4_000)
    pr_intent_context_truncated: bool | None = Field(default=None, strict=True)
    intent_assessment_contract: Literal["structured-intent-v1", "structured-intent-v2", "structured-intent-v3", "structured-intent-v4"] | None = None
    discussion_manifest_hash: str | None = None
    discussion_context: list[dict] | None = None
    discussion_profile: dict | None = None
    discussion_recheck: bool | None = None

    @model_serializer(mode="wrap")
    def serialize_strategy(self, handler):
        value = handler(self)
        if not self.review_strategy:
            value.pop("review_strategy", None)
        if self.pr_intent_context is None:
            value.pop("pr_intent_context", None)
        if self.pr_intent_context_truncated is None:
            value.pop("pr_intent_context_truncated", None)
        if self.intent_assessment_contract is None:
            value.pop("intent_assessment_contract", None)
        for key in ("discussion_manifest_hash", "discussion_context", "discussion_profile", "discussion_recheck"):
            if getattr(self, key) is None:
                value.pop(key, None)
        return value

    @property
    def readable_scope_hash(self) -> str:
        return hashlib.sha256(json.dumps(sorted(self.readable_scope_files), separators=(",", ":"),
                                        ensure_ascii=False).encode()).hexdigest()

    @model_validator(mode="after")
    def different_snapshot_revisions(self) -> "ReviewRunSpec":
        if self.snapshot_head_sha == self.snapshot_base_sha:
            raise ValueError("snapshot head and base must differ")
        for path in self.readable_scope_files:
            _path(path)
        if len(set(self.readable_scope_files)) != len(self.readable_scope_files):
            raise ValueError("duplicate frozen readable path")
        return self


class ReviewGoal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    goal_id: str = Field(min_length=1, max_length=160)
    unit_id: str = Field(min_length=1, max_length=160)
    kind: WorkItemKind
    target_ref: str = Field(min_length=1, max_length=1_000)
    required: bool = True
    origin: Literal["initial", "derived"] = "initial"
    target_kind: Literal["source_range", "file_change", "base_source_range"] = "source_range"
    start_line: int = Field(default=1, ge=1)
    end_line: int = Field(default=1, ge=1)
    unsupported_reason: str | None = None

    @property
    def base_side(self) -> bool:
        """目标范围以删除前源码行号为准；范围与证据必须在 Base 侧核对。"""
        return self.target_kind == "base_source_range"

    @model_validator(mode="after")
    def valid_source_range(self) -> "ReviewGoal":
        _path(self.target_ref)
        if self.end_line < self.start_line:
            raise ValueError("goal source range is invalid")
        return self


class ReviewWorkItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    work_item_id: str = Field(min_length=1, max_length=160)
    kind: WorkItemKind
    goal_ids: list[str] = Field(min_length=1, max_length=40)
    scope_files: list[RelativePath] = Field(default_factory=list, max_length=40)
    readable_scope_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    state: WorkItemState = WorkItemState.ready
    revision: int = Field(default=0, ge=0)
    attempt_count: int = Field(default=0, ge=0)
    format_corrections: int = Field(default=0, ge=0, le=1)
    unit_id: str | None = Field(default=None, max_length=160)
    sequence: int = Field(default=0, ge=0)
    scheduling_class: Literal["basic", "continuation", "verification", "investigation"] = "basic"
    started: bool = False
    last_checked_budget_revision: int = -1
    last_checked_obligation_revision: int = -1
    reason: str | None = None
    proposition: str | None = Field(default=None, min_length=1, max_length=2_000)
    related_goal_ids: list[str] = Field(default_factory=list, max_length=40)
    required_evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    counter_evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    evidence_requests: list[RuntimeReadArguments] = Field(default_factory=list, max_length=20)
    verification_candidate: Candidate | None = None
    generation: int = Field(default=0, ge=0, le=1)
    origin_work_item_id: str | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> "ReviewWorkItem":
        self.scope_files = [_path(path) for path in self.scope_files]
        if len(set(self.goal_ids)) != len(self.goal_ids):
            raise ValueError("work item has duplicate goal identity")
        return self


class RuntimeReadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["read"] = "read"
    tool: Literal["file_read", "file_find", "code_search", "file_read_diff"]
    arguments: RuntimeReadArguments | FileFindRequest | CodeSearchRequest | FileReadDiffRequest
    purpose: str = Field(min_length=1, max_length=1_000)

    @model_validator(mode="before")
    @classmethod
    def bind_tool_arguments(cls, value):
        if not isinstance(value, dict):
            return value
        schemas = {"file_read": RuntimeReadArguments, "file_find": FileFindRequest,
                   "code_search": CodeSearchRequest, "file_read_diff": FileReadDiffRequest}
        if value.get("tool") in schemas:
            value = dict(value)
            value["arguments"] = schemas[value["tool"]].model_validate(value.get("arguments"))
        return value

    @model_validator(mode="after")
    def exact_tool_schema(self):
        expected = {"file_read": RuntimeReadArguments, "file_find": FileFindRequest,
                    "code_search": CodeSearchRequest, "file_read_diff": FileReadDiffRequest}[self.tool]
        if not isinstance(self.arguments, expected):
            raise ValueError("tool arguments schema mismatch")
        return self


class DiscussionRuntimeReadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["read"] = "read"
    tool: Literal["discussion_read"]
    arguments: DiscussionReadArguments
    purpose: str = Field(min_length=1, max_length=1_000)


class IntentAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    author_quote: str = Field(min_length=1, max_length=4_000)
    actual_behavior: str = Field(min_length=1, max_length=2_000)
    alignment: Literal["supported", "contradicted", "insufficient", "not_applicable"]
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    limitations: list[str] = Field(default_factory=list, max_length=20)
    author_goal_summary: str | None = Field(default=None, min_length=1, max_length=240)

    @model_serializer(mode="wrap")
    def serialize_goal_summary(self, handler):
        value = handler(self)
        if self.author_goal_summary is None:
            value.pop("author_goal_summary", None)
        return value


class ExaminationSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: str = Field(min_length=1, max_length=160)
    disposition: ExaminationDisposition
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    reason: str = Field(min_length=1, max_length=2_000)
    intent_assessments: list[IntentAssessment] = Field(default_factory=list, max_length=24)

    @model_serializer(mode="wrap")
    def serialize_assessments(self, handler):
        value = handler(self)
        if not self.intent_assessments:
            value.pop("intent_assessments", None)
        return value


class CandidateSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim: str = Field(min_length=1, max_length=2_000)
    primary_anchor: str = Field(min_length=1, max_length=160)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)
    affected_behavior: str = Field(default="", max_length=1_000)
    failure_scenario: str = Field(default="", max_length=2_000)
    recommendation: str = Field(default="", max_length=2_000)
    category: IssueCategory = IssueCategory.correctness
    severity: Severity = Severity.medium
    evidence_requests: list[RuntimeReadArguments] = Field(default_factory=list, max_length=20)


class LeadSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposition: str = Field(min_length=1, max_length=2_000)
    related_target_ids: list[str] = Field(min_length=1, max_length=20)
    missing_evidence: list[str] = Field(min_length=1, max_length=20)
    evidence_requests: list[RuntimeReadArguments] = Field(default_factory=list, max_length=20)


class InvestigationSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: InvestigationAnswer
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    limitations: list[str] = Field(default_factory=list, max_length=20)
    reason: str = Field(min_length=1, max_length=2_000)


class DiscussionInvestigationSubmission(InvestigationSubmission):
    resolution: Literal["still_present", "not_present", "fixed", "insufficient_evidence", "background"]
    severity: Literal["critical", "high", "medium", "low"] | None = None
    affected_behavior: str | None = Field(default=None, min_length=1, max_length=2_000)
    recommendation: str | None = Field(default=None, min_length=1, max_length=2_000)


class VerificationSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: str = Field(min_length=1, max_length=160)
    decision: VerificationDecision
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    reason: str = Field(min_length=1, max_length=2_000)
    discussion_matches: list[DiscussionMatch] = Field(default_factory=list, max_length=10)

    @model_serializer(mode="wrap")
    def serialize_discussions(self, handler):
        value = handler(self)
        if not self.discussion_matches:
            value.pop("discussion_matches", None)
        return value


class PublicationTextNarrowing(BaseModel):
    """只能收窄非核心展示字段；claim 与 failure_scenario 保持服务器原文。"""
    model_config = ConfigDict(extra="forbid")
    affected_behavior: str | None = Field(default=None, min_length=3, max_length=1_000)
    recommendation: str | None = Field(default=None, min_length=3, max_length=2_000)
    basis: str = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def needs_change(self):
        if self.affected_behavior is None and self.recommendation is None:
            raise ValueError("narrowing requires a non-core text change")
        return self


class PublicationVerificationSubmission(VerificationSubmission):
    subject_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    publication_action: Literal["retain", "narrow", "none"]
    narrowing: PublicationTextNarrowing | None = None

    @model_validator(mode="after")
    def bind_publication_decision(self):
        if self.decision == VerificationDecision.supported:
            if self.publication_action == "none":
                raise ValueError("supported requires retain or narrow")
        elif self.publication_action != "none":
            raise ValueError("refuted or unknown premises cannot inherit publication")
        if (self.publication_action == "narrow") != (self.narrowing is not None):
            raise ValueError("narrowing must match publication action")
        return self


class RuntimeSubmitResponse(BaseModel):
    """inspect 专用提交；调查与核验使用各自的小型 schema。"""
    model_config = ConfigDict(extra="forbid")
    kind: Literal["submit"] = "submit"
    examinations: list[ExaminationSubmission] = Field(default_factory=list, max_length=40)
    candidates: list[CandidateSubmission] = Field(default_factory=list, max_length=20)
    leads: list[LeadSubmission] = Field(default_factory=list, max_length=20)


class InvestigationSubmitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["submit"] = "submit"
    investigation: InvestigationSubmission
    candidates: list[CandidateSubmission] = Field(default_factory=list, max_length=20)
    leads: list[LeadSubmission] = Field(default_factory=list, max_length=20)


class DiscussionInvestigationSubmitResponse(InvestigationSubmitResponse):
    investigation: DiscussionInvestigationSubmission


class VerificationSubmitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["submit"] = "submit"
    verification: VerificationSubmission


class PublicationVerificationSubmitResponse(VerificationSubmitResponse):
    verification: PublicationVerificationSubmission
    candidates: list[CandidateSubmission] = Field(default_factory=list, max_length=20)


RuntimeResponse = Annotated[RuntimeReadResponse | RuntimeSubmitResponse, Field(discriminator="kind")]


class ExaminationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result_id: str
    goal_id: str
    request_id: str
    snapshot_head_sha: Revision
    disposition: ExaminationDisposition
    evidence_ids: list[str] = Field(default_factory=list)
    reason: str
    intent_assessments: list[IntentAssessment] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def serialize_assessments(self, handler):
        value = handler(self)
        if not self.intent_assessments:
            value.pop("intent_assessments", None)
        return value


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: str
    claim: str
    primary_anchor: str
    supporting_evidence: list[str]
    source_result_ids: list[str]
    affected_behavior: str = ""
    failure_scenario: str = ""
    recommendation: str = ""
    category: IssueCategory = IssueCategory.correctness
    severity: Severity = Severity.medium
    evidence_requests: list[RuntimeReadArguments] = Field(default_factory=list)


class ProposedLead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lead_id: str
    proposition: str
    related_targets: list[str]
    missing_evidence: list[str]
    origin_result_id: str
    evidence_requests: list[RuntimeReadArguments] = Field(default_factory=list)


class InvestigationResult(InvestigationSubmission):
    work_item_id: str
    proposition: str
    request_id: str
    attempt_id: str
    snapshot_head_sha: Revision


class DiscussionInvestigationResult(InvestigationResult, DiscussionInvestigationSubmission):
    pass


class VerificationResult(VerificationSubmission):
    work_item_id: str
    request_id: str
    attempt_id: str
    snapshot_head_sha: Revision
    subject_hash: str | None = None
    publication_action: Literal["retain", "narrow", "none"] | None = None
    narrowing: PublicationTextNarrowing | None = None
    discussion_manifest_hash: str | None = None

    @model_serializer(mode="wrap")
    def serialize_discussion_result(self, handler):
        value = handler(self)
        if not self.discussion_matches:
            value.pop("discussion_matches", None)
        if self.discussion_manifest_hash is None:
            value.pop("discussion_manifest_hash", None)
        return value


ReviewWorkItem.model_rebuild()


class TaskRuntimeReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    engine: Literal["task_review_v1"] = "task_review_v1"
    coverage_version: Literal["task-goals-v1"] = "task-goals-v1"
    execution_status: Literal["running", "completed", "failed", "cancelled"]
    termination_reason: str
    failure_summary: str | None = None
    readable_scope_hash: str | None = None
    readable_file_count: int | None = Field(default=None, ge=0)
    scope_complete: bool = False
    initial_scope_coverage: list[dict] = Field(default_factory=list)
    admitted_investigations: list[dict] = Field(default_factory=list)
    unscheduled_leads: list[dict] = Field(default_factory=list)
    relationship_hints: list[dict] = Field(default_factory=list)
    findings: list[dict] = Field(default_factory=list)
    usage_integrity: Literal["complete", "unknown", "inconsistent"] = "unknown"
    sent_calls: int = Field(default=0, ge=0)
    settled_tokens: int = Field(default=0, ge=0)
    unknown_attempts: int = Field(default=0, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    work_items: list[dict] = Field(default_factory=list)
    diagnostics: list[dict] = Field(default_factory=list)
    # 历史讨论补充阶段的独立计量；不改变上方当前 Diff 主审查的字段与完整性。
    discussion_usage: DiscussionUsage | None = None
    pr_intent_context: str | None = Field(default=None, min_length=1, max_length=4_000)
    pr_intent_context_truncated: bool | None = Field(default=None, strict=True)

    @model_serializer(mode="wrap")
    def serialize_intent_context(self, handler):
        value = handler(self)
        if self.pr_intent_context is None:
            value.pop("pr_intent_context", None)
        if self.pr_intent_context_truncated is None:
            value.pop("pr_intent_context_truncated", None)
        if self.discussion_usage is None:
            value.pop("discussion_usage", None)
        return value


class TaskRuntimeRetryResult(BaseModel):
    engine: Literal["task_review_v1"] = "task_review_v1"
    unit_id: str
    requeued_work_item_ids: list[str]
    task_runtime_report: TaskRuntimeReport
