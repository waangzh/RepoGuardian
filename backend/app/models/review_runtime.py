"""task_review_v1 的最小协议和服务端状态模型。

模型只提供当前任务的取证动作或结果提交；目标身份、快照、证据和完成状态
由服务端绑定。此模块不复用旧 UnitReviewRecord/question_updates 协议。
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from app.models.review import (FileReadRequest, FileFindRequest, CodeSearchRequest,
                              FileReadDiffRequest, IssueCategory, Severity)


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
    max_tokens: int = Field(ge=0)
    max_calls: int = Field(ge=0)

    @model_validator(mode="after")
    def different_snapshot_revisions(self) -> "ReviewRunSpec":
        if self.snapshot_head_sha == self.snapshot_base_sha:
            raise ValueError("snapshot head and base must differ")
        return self


class ReviewGoal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    goal_id: str = Field(min_length=1, max_length=160)
    unit_id: str = Field(min_length=1, max_length=160)
    kind: WorkItemKind
    target_ref: str = Field(min_length=1, max_length=1_000)
    required: bool = True
    origin: Literal["initial", "derived"] = "initial"
    target_kind: Literal["source_range", "file_change"] = "source_range"
    start_line: int = Field(default=1, ge=1)
    end_line: int = Field(default=1, ge=1)

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


class ExaminationSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: str = Field(min_length=1, max_length=160)
    disposition: ExaminationDisposition
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    reason: str = Field(min_length=1, max_length=2_000)


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


class VerificationSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: str = Field(min_length=1, max_length=160)
    decision: VerificationDecision
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    reason: str = Field(min_length=1, max_length=2_000)


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


class VerificationSubmitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["submit"] = "submit"
    verification: VerificationSubmission


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


class VerificationResult(VerificationSubmission):
    work_item_id: str
    request_id: str
    attempt_id: str
    snapshot_head_sha: Revision


ReviewWorkItem.model_rebuild()
