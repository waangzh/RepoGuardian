"""证据锚点、清单与工作集模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from app.core.paths import validate_repo_relative_path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅类型检查时导入，避免与 task 模块的运行时循环
    from app.models.review.task import ReviewUnitResult
from app.models.review.common import EvidenceProvenance, EvidenceResolutionMethod, EvidenceResolutionStatus
from app.models.review.diff import FileChangeEvidence

class EvidenceCandidate(BaseModel):
    """未唯一定位时保留的只读候选位置，便于调试和人工确认。"""

    model_config = ConfigDict(extra="forbid")

    file_path: str
    side: Literal["head", "base"]
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    hunk_id: str | None = None


class EvidenceBranchClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    condition_code: str = Field(min_length=1, max_length=500)
    arm: Literal["if", "else", "both"]

    @field_validator("condition_code")
    @classmethod
    def normalize_condition_header(cls, value: str) -> str:
        value = value.strip()
        for prefix in ("if ", "elif "):
            if value.startswith(prefix):
                return value[len(prefix):].removesuffix(":").strip()
        return value


class EvidenceAnchor(BaseModel):
    """模型提供代码片段，位置与哈希只能由服务端解析写入。"""

    model_config = ConfigDict(extra="forbid")

    file_path: str
    existing_code: str = Field(min_length=1, max_length=8_000)
    symbol: str | None = None
    expected_side: Literal["head", "base", "either"] = "head"
    expected_hunk_id: str | None = None
    branch_claim: EvidenceBranchClaim | None = None
    context_before: list[str] = Field(default_factory=list, max_length=12)
    context_after: list[str] = Field(default_factory=list, max_length=12)
    resolved_start_line: int | None = Field(default=None, ge=1)
    resolved_end_line: int | None = Field(default=None, ge=1)
    resolution_method: EvidenceResolutionMethod = EvidenceResolutionMethod.unresolved
    resolution_status: EvidenceResolutionStatus = EvidenceResolutionStatus.unresolved
    provenance: EvidenceProvenance | None = None
    match_count: int = Field(default=0, ge=0)
    anchor_hash: str | None = None
    resolved_side: Literal["head", "base"] | None = None
    candidate_locations: list[EvidenceCandidate] = Field(default_factory=list)
    unresolved_reason: str | None = None

    @field_validator("file_path")
    @classmethod
    def validate_file_path(cls, value: str) -> str:
        return validate_repo_relative_path(value)

    @field_validator("context_before", "context_after")
    @classmethod
    def validate_context(cls, values: list[str]) -> list[str]:
        if any(not isinstance(value, str) or len(value) > 1_000 for value in values):
            raise ValueError("anchor context lines must be short strings")
        return values

    @model_validator(mode="after")
    def validate_resolved_range(self) -> "EvidenceAnchor":
        if (self.resolved_start_line is None) != (self.resolved_end_line is None):
            raise ValueError("resolved line range must be complete")
        if (
            self.resolved_start_line is not None
            and self.resolved_end_line is not None
            and self.resolved_end_line < self.resolved_start_line
        ):
            raise ValueError("resolved_end_line must not precede resolved_start_line")
        return self


class AuditableReasoningSummary(BaseModel):
    """面向审计者的简短因果摘要，不包含隐藏思维链。"""

    model_config = ConfigDict(extra="forbid")

    change: str = Field(min_length=1, max_length=500)
    invariant: str = Field(min_length=1, max_length=500)
    violation: str = Field(min_length=1, max_length=500)
    consequence: str = Field(min_length=1, max_length=500)


class ContextSnippet(BaseModel):
    """代码上下文片段（CodeSearch 检索产物）。"""
    file: str
    start_line: int
    end_line: int
    content: str
    relevance: str         # direct / caller / test / adjacent
    symbol: str | None = None
    review_unit_id: str | None = None
    source: str | None = None
    derived_from_evidence_id: str | None = None
    distance: int | None = Field(default=None, ge=0, le=2)
    confidence: float | None = Field(default=None, ge=0, le=1)
    why_retrieved: str | None = None
    truncated: bool = False
    requested_end_line: int | None = Field(default=None, ge=1)
    total_lines: int | None = Field(default=None, ge=0)


class UnitEvidenceReference(BaseModel):
    """服务端生成的证据目录项，绑定当前仓库快照。"""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    file_path: str
    source: Literal["diff", "context", "file_change", "repository_tree"]
    start_line: int = Field(ge=0)
    end_line: int = Field(ge=0)
    head_sha: str
    base_sha: str = ""
    content_hash: str
    derived_from_evidence_id: str | None = None
    file_change: FileChangeEvidence | None = None

    @model_serializer(mode="wrap")
    def serialize_reference(self, handler):
        result = handler(self)
        if self.file_change is None:
            result.pop("file_change", None)
        return result

    @field_validator("file_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_repo_relative_path(value)

    @model_validator(mode="after")
    def ordered_lines(self) -> "UnitEvidenceReference":
        if self.end_line < self.start_line:
            raise ValueError("evidence line range is reversed")
        if self.source == "repository_tree" and (self.start_line or self.end_line):
            raise ValueError("repository tree evidence has no source line anchor")
        if self.source == "file_change":
            from app.evidence.file_change_evidence import validate_file_change_evidence
            if self.file_change is None or self.start_line or self.end_line:
                raise ValueError("file change evidence has no line anchor")
            validate_file_change_evidence(self.file_change, self.head_sha, self.base_sha, self.file_path)
            if self.id != self.file_change.id or self.content_hash != self.file_change.content_hash:
                raise ValueError("file change evidence reference mismatch")
        elif self.file_change is not None:
            raise ValueError("line evidence cannot carry file change metadata")
        return self


class UnitTargetCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field(min_length=1, max_length=1_000)
    status: Literal["checked", "unresolved", "not_checked"]
    evidence_ids: list[str] = Field(default_factory=list, max_length=12)
    reason: str = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def checked_requires_evidence(self) -> "UnitTargetCheck":
        if self.status == "checked" and not self.evidence_ids:
            raise ValueError("checked target requires evidence")
        return self


class UnitHypothesisCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hypothesis_id: str = Field(min_length=1, max_length=100)
    status: Literal["supported", "refuted", "unresolved"]
    evidence_ids: list[str] = Field(default_factory=list, max_length=12)
    reason: str = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def conclusion_requires_evidence(self) -> "UnitHypothesisCheck":
        if self.status != "unresolved" and not self.evidence_ids:
            raise ValueError("hypothesis conclusion requires evidence")
        return self


class UnitContractDependency(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_path: str
    symbol: str | None = None
    assumption: str = Field(min_length=1, max_length=1_000)
    status: Literal["verified", "unresolved", "conflicting"]
    evidence_ids: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("file_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_repo_relative_path(value)

    @model_validator(mode="after")
    def conclusion_requires_evidence(self) -> "UnitContractDependency":
        if self.status != "unresolved" and not self.evidence_ids:
            raise ValueError("contract conclusion requires evidence")
        return self


class UnitUnresolvedQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(default=None, min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=1_000)
    affected_files: list[str] = Field(default_factory=list, max_length=12)
    evidence_ids: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("affected_files")
    @classmethod
    def validate_paths(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(validate_repo_relative_path(value) for value in values))


class UnitQuestionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question_id: str = Field(min_length=1, max_length=100)
    status: Literal["resolved", "superseded"]
    reason: str = Field(min_length=1, max_length=1_000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=12)
    replacement_id: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def valid_transition(self) -> "UnitQuestionUpdate":
        if self.status == "resolved" and (not self.evidence_ids or self.replacement_id):
            raise ValueError("resolved question requires evidence and no replacement")
        if self.status == "superseded" and (not self.replacement_id or self.replacement_id == self.question_id):
            raise ValueError("superseded question requires a distinct replacement")
        return self


class UnitFileChangeCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: str
    impact_status: Literal["checked", "unresolved", "not_checked"]
    evidence_ids: list[str] = Field(default_factory=list, max_length=12)
    reason: str = Field(min_length=1, max_length=1_000)


class UnitFileChangeCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metadata_verified: bool = False
    impact_status: Literal["pending", "checked", "unresolved", "not_checked"] = "pending"
    evidence_ids: list[str] = Field(default_factory=list)
    reason: str | None = None


class UnitObligationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    obligation_id: str = Field(min_length=1, max_length=100)
    status: Literal["verified", "resolved"]
    evidence_ids: list[str] = Field(min_length=1, max_length=12)
    reason: str = Field(min_length=1, max_length=1_000)


class DiffManifestHunk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    file_path: str
    hunk_id: str
    old_start: int = 0
    old_length: int = 0
    new_start: int = 0
    new_length: int = 0
    line_count: int = Field(ge=0)
    content_hash: str
    evidence_id: str | None = None
    evidence_kind: Literal["diff", "file_change"] = "diff"


class UnitDiffManifest(BaseModel):
    """不可引用的完整变更清单；正文仅存在于活动证据中。"""
    model_config = ConfigDict(extra="forbid")
    version: Literal["unit-diff-manifest-v1"] = "unit-diff-manifest-v1"
    review_unit_id: str
    snapshot: dict[str, str]
    input_hash: str
    files: list[dict[str, Any]]
    hunks: list[DiffManifestHunk]


class UnitHunkCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["pending", "reviewed", "needs_followup", "unsupported"] = "pending"
    batch_ids: list[str] = Field(default_factory=list)
    reviewed_ranges: list[list[int]] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    reason: str | None = None


class UnitCoverageCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["pending", "checked", "unresolved", "supported", "refuted", "verified", "conflicting"] = "pending"
    required: bool = True
    evidence_ids: list[str] = Field(default_factory=list)
    reason: str | None = None


class UnitQuestionCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str
    affected_files: list[str]
    evidence_ids: list[str]
    status: Literal["pending", "resolved", "superseded"] = "pending"


class UnitCoverageLedger(BaseModel):
    """服务端推导的覆盖状态，checked/reviewed 不表示正确性证明。"""
    model_config = ConfigDict(extra="forbid")
    version: Literal["unit-coverage-ledger-v1"] = "unit-coverage-ledger-v1"
    review_unit_id: str
    manifest_hash: str
    snapshot: dict[str, str]
    diff_hunks: dict[str, UnitHunkCoverage]
    targets: dict[str, UnitCoverageCheck] = Field(default_factory=dict)
    hypotheses: dict[str, UnitCoverageCheck] = Field(default_factory=dict)
    questions: dict[str, UnitQuestionCoverage] = Field(default_factory=dict)
    dependencies: dict[str, UnitCoverageCheck] = Field(default_factory=dict)
    supporting_evidence: dict[str, UnitEvidenceReference] = Field(default_factory=dict)
    file_changes: dict[str, UnitFileChangeCoverage] = Field(default_factory=dict)


class UnitActiveHunk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest_id: str
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=0)
    evidence_id: str | None = None
    content_hash: str
    source_hunk_hash: str


class UnitActiveEvidenceSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    batch_id: str
    manifest_hash: str
    hunks: list[UnitActiveHunk]
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    file_change_evidence_ids: list[str] = Field(default_factory=list)


class UnitStoredEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference: UnitEvidenceReference
    snippet: ContextSnippet
    discovered_order: int = Field(ge=0)
    request_fingerprints: list[str] = Field(default_factory=list)


class UnitEvidenceStore(BaseModel):
    """窗口外的 supporting evidence；归档条目不自动成为当前可引用证据。"""
    model_config = ConfigDict(extra="forbid")
    version: Literal["unit-evidence-store-v1"] = "unit-evidence-store-v1"
    review_unit_id: str
    snapshot: dict[str, str]
    revision: int = Field(default=0, ge=0)
    entries: dict[str, UnitStoredEvidence] = Field(default_factory=dict)
    request_groups: dict[str, list[str]] = Field(default_factory=dict)
    validation_warnings: list[str] = Field(default_factory=list)


class UnitContextSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    stored_count: int = Field(ge=0)
    active_evidence_ids: list[str]
    admitted_evidence_ids: list[str] = Field(default_factory=list)
    evicted_evidence_ids: list[str] = Field(default_factory=list)
    reactivated_evidence_ids: list[str] = Field(default_factory=list)
    pinned_evidence_ids: list[str] = Field(default_factory=list)
    priorities: dict[str, str] = Field(default_factory=dict)


class UnitDiffBatch(BaseModel):
    """父 Unit 内部工作集；不是额外的顶层 Unit。"""

    model_config = ConfigDict(extra="forbid")
    id: str
    parent_unit_id: str
    input_hash: str
    version: str
    ranges: list[dict[str, Any]]
    admission: dict[str, Any]
    status: Literal["pending", "completed", "partial", "skipped", "deferred_budget", "failed"] = "pending"
    batch_execution_status: Literal[
        "pending", "completed", "no_new_context", "partial", "skipped", "deferred_budget", "failed"
    ] = "pending"
    target_coverage: Literal["complete", "partial", "unknown"] = "unknown"
    contract_coverage: Literal["complete", "pending", "unknown"] = "unknown"
    reason: str | None = None
    result: "ReviewUnitResult | None" = None
    # 本批次交接时的未决义务与“可携带但未携带”的证据路径；旧快照没有该字段。
    contract_handoff: dict[str, Any] | None = None

    @model_validator(mode="after")
    def normalize_execution_status(self) -> "UnitDiffBatch":
        """兼容旧快照，并将无进展终止与目标覆盖分开保存。"""
        if self.batch_execution_status == "pending" and self.status != "pending":
            self.batch_execution_status = (
                "no_new_context"
                if self.status == "partial" and self.reason == "review_step_no_evidence_progress"
                else self.status
            )
        return self


class UnitBudgetRejection(BaseModel):
    """服务端发送准入拒绝；与工作集数量无关，恢复时仍可重查。"""

    model_config = ConfigDict(extra="allow")
    kind: Literal["budget"] = "budget"
    reason: str = Field(min_length=1, max_length=1000)
    can_defer: bool = False
    budget_revision: int = Field(ge=0)
