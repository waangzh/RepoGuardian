"""问题、动作与跨 Unit 协调模型。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.core.paths import validate_repo_relative_path
from app.models.review.common import AgentActionName, CommandId, CommentPlacement, FixRisk, IssueCategory, IssueStatus, Severity
from app.models.review.evidence import AuditableReasoningSummary, EvidenceAnchor, UnitContractDependency, UnitEvidenceReference, UnitFileChangeCheck, UnitHypothesisCheck, UnitObligationUpdate, UnitQuestionUpdate, UnitTargetCheck, UnitUnresolvedQuestion
from app.models.review.requests import CodeSearchRequest, ContextRetrievalPlan, ExecutionBudget, FileFindRequest, FileReadDiffRequest, FileReadRequest, HumanReviewRequest

class ResolvedIssueLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    side: Literal["head", "base"]
    hunk_id: str | None = None


class ReviewIssue(BaseModel):
    """可追溯的审查问题；发布位置完全来自服务端证据解析。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex)
    review_unit_id: str
    title: str
    category: IssueCategory
    severity: Severity
    confidence: float = Field(ge=0, le=1)
    affected_behavior: str = Field(min_length=3, max_length=1_000)
    failure_scenario: str = Field(min_length=3, max_length=2_000)
    reasoning_summary: AuditableReasoningSummary | None = None
    recommendation: str = Field(min_length=1, max_length=2_000)
    primary_evidence: EvidenceAnchor
    supporting_evidence: list[EvidenceAnchor] = Field(default_factory=list, max_length=12)
    assumptions: list[str] = Field(default_factory=list, max_length=8)
    related_tests: list[str] = Field(default_factory=list, max_length=12)
    requires_human_confirmation: bool = False
    auto_fix_eligible: bool = False
    fix_risk: FixRisk = FixRisk.low
    status: IssueStatus = IssueStatus.candidate
    placement: CommentPlacement = CommentPlacement.suppressed
    unresolved_reason: str | None = None
    resolved_location: ResolvedIssueLocation | None = None
    source_review_unit_ids: list[str] = Field(default_factory=list)
    source_issue_ids: list[str] = Field(default_factory=list)

    def for_review_unit(self, unit_id: str) -> "ReviewIssue":
        """服务端重绑定所属 Unit，同时投影真实来源；不抹掉其他 Unit 来源。"""
        sources = [unit_id if source in {"unassigned", self.review_unit_id} else source
                   for source in self.source_review_unit_ids]
        return self.model_copy(update={"review_unit_id": unit_id,
            "source_review_unit_ids": list(dict.fromkeys([*sources, unit_id]))})

    @field_validator("assumptions", "related_tests")
    @classmethod
    def validate_short_text_lists(cls, values: list[str]) -> list[str]:
        if any(not isinstance(value, str) or not value.strip() or len(value) > 500 for value in values):
            raise ValueError("issue text lists must contain short non-empty values")
        return list(dict.fromkeys(value.strip() for value in values))

    @model_validator(mode="after")
    def restrict_auto_fix_to_resolved_evidence(self) -> "ReviewIssue":
        if self.auto_fix_eligible and self.requires_human_confirmation:
            raise ValueError("issues requiring human confirmation are not auto-fix eligible")
        if not self.source_review_unit_ids:
            self.source_review_unit_ids = [self.review_unit_id]
        else:
            self.source_review_unit_ids = list(dict.fromkeys(self.source_review_unit_ids))
        if not self.source_issue_ids:
            self.source_issue_ids = [self.id]
        else:
            self.source_issue_ids = list(dict.fromkeys(self.source_issue_ids))
        return self


class DeterministicIssueCheck(BaseModel):
    """Issue 进入独立 verifier 前的服务端确定性准入结论。"""

    model_config = ConfigDict(extra="forbid")

    issue_id: str
    passed: bool
    reasons: list[str] = Field(default_factory=list)
    normalized_severity: Severity | None = None


class IssueMetrics(BaseModel):
    """Issue 筛选、验证和聚合阶段的任务级指标。"""

    model_config = ConfigDict(extra="forbid")

    candidate_issue_count: int = Field(default=0, ge=0)
    deterministic_drop_count: int = Field(default=0, ge=0)
    verifier_drop_count: int = Field(default=0, ge=0)
    needs_human_count: int = Field(default=0, ge=0)
    unresolved_count: int = Field(default=0, ge=0)
    duplicate_count: int = Field(default=0, ge=0)
    confirmed_count: int = Field(default=0, ge=0)
    severity_adjustment_count: int = Field(default=0, ge=0)
    verifier_call_count: int = Field(default=0, ge=0)
    verifier_token_count: int = Field(default=0, ge=0)


class AgentAction(BaseModel):
    """LLM 决策节点产出的下一步操作指令。"""
    action: AgentActionName
    reason: str                                    # 选择该操作的中文理由
    target_issue_ids: list[str] = Field(default_factory=list)
    tool_args: dict[str, Any] = Field(default_factory=dict)
    human_request: HumanReviewRequest | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def reject_free_form_shell_command(self) -> "AgentAction":
        """模型可选命令 ID，但不能把任意 Shell 命令带入工具调用。"""
        if "command" in self.tool_args:
            raise ValueError("tool_args.command is not supported; use command_id")
        command_id = self.tool_args.get("command_id")
        if command_id is not None:
            try:
                CommandId(command_id)
            except ValueError as exc:
                raise ValueError(f"unknown command_id: {command_id}") from exc
        if self.action == AgentActionName.retrieve_context:
            if set(self.tool_args) != {"plan"}:
                raise ValueError("retrieve_context requires tool_args.plan only")
            plan = ContextRetrievalPlan.model_validate(self.tool_args["plan"])
            self.tool_args = {"plan": plan.model_dump(mode="json")}
        elif self.action in {
            AgentActionName.file_read,
            AgentActionName.file_find,
            AgentActionName.code_search,
            AgentActionName.file_read_diff,
        }:
            if set(self.tool_args) != {"request"}:
                raise ValueError(f"{self.action.value} requires tool_args.request only")
            request_type = {
                AgentActionName.file_read: FileReadRequest,
                AgentActionName.file_find: FileFindRequest,
                AgentActionName.code_search: CodeSearchRequest,
                AgentActionName.file_read_diff: FileReadDiffRequest,
            }[self.action]
            request = request_type.model_validate(self.tool_args["request"])
            self.tool_args = {"request": request.model_dump(mode="json")}
        elif self.action in {AgentActionName.report_issue, AgentActionName.task_done}:
            if self.tool_args:
                raise ValueError(f"{self.action.value} does not accept tool_args")
        elif self.tool_args:
            raise ValueError(f"tool_args are not allowed for action '{self.action.value}'")

        if self.action == AgentActionName.request_human:
            if self.human_request is None:
                raise ValueError("request_human requires a structured human_request")
        elif self.human_request is not None:
            raise ValueError("human_request is only allowed for request_human")
        return self


class UnitRiskHypothesis(BaseModel):
    """Unit 规划阶段提出的待验证风险，不代表已经确认的 Issue。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    category: IssueCategory
    priority: Literal["high", "medium", "low"]
    description: str = Field(min_length=1, max_length=1_000)
    affected_files: list[str] = Field(default_factory=list, max_length=12)
    affected_symbols: list[str] = Field(default_factory=list, max_length=20)
    evidence_needed: list[str] = Field(default_factory=list, max_length=12)
    retrieval_suggestions: list[ContextRetrievalPlan] = Field(default_factory=list, max_length=4)
    completion_criteria: str = Field(min_length=1, max_length=500)

    @field_validator("affected_files")
    @classmethod
    def validate_affected_files(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(validate_repo_relative_path(value) for value in values))


class UnitReviewPlan(BaseModel):
    """一次 Unit 审查的结构化风险与证据规划。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["unit-review-plan-v1"] = "unit-review-plan-v1"
    change_summary: str = Field(min_length=1, max_length=1_500)
    review_objectives: list[str] = Field(min_length=1, max_length=12)
    risk_hypotheses: list[UnitRiskHypothesis] = Field(default_factory=list, max_length=12)
    coverage_targets: list[str] = Field(default_factory=list, max_length=20)
    initial_action: AgentAction

    @model_validator(mode="after")
    def require_unique_hypothesis_ids(self) -> "UnitReviewPlan":
        ids = [item.id for item in self.risk_hypotheses]
        if len(ids) != len(set(ids)):
            raise ValueError("risk hypothesis ids must be unique")
        return self


class AgentEvent(BaseModel):
    """Agent 决策事件日志条目。"""
    action: AgentActionName | str
    reason: str
    status: str         # selected / completed / failed
    message: str | None = None
    review_unit_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReviewUnitToolEvent(BaseModel):
    review_unit_id: str
    tool: str
    status: str
    result_count: int = Field(default=0, ge=0)
    detail: str | None = None


class TestRunResult(BaseModel):
    """命令行工具执行结果（静态分析 / 测试共用）。"""
    tool: str            # 工具名：static_analyzer / test_runner
    command: str         # 执行的命令
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    passed: bool         # exit_code == 0
    duration: float = 0.0


class FailureFingerprint(BaseModel):
    """由受控验证输出提取的、可集合比较的单个失败。"""

    tool: str
    identity: str
    test_node_id: str | None = None
    error_type: str | None = None
    file_path: str | None = None
    line_no: int | None = None
    column: int | None = None
    rule_code: str | None = None
    message: str | None = None
    normalized_summary: str


class CommandSpec(BaseModel):
    """仅由服务端适配器注册的命令定义。"""

    command_id: CommandId
    argv: tuple[str, ...] = Field(min_length=1)
    tool: str
    timeout_seconds: int = Field(default=60, gt=0, le=600)


class ProjectProfile(BaseModel):
    """项目适配器检测出的、可安全公开的项目元数据。"""

    adapter_id: str
    language: str
    detected_files: list[str] = Field(default_factory=list)
    validation_command_ids: list[CommandId] = Field(default_factory=list)


class UnitReviewRecord(BaseModel):
    """模型提供的语义检查记录；引用由执行器核对，checked 不等于正确性证明。"""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["unit-review-record-v1"] = "unit-review-record-v1"
    change_summary: str = Field(min_length=1, max_length=1_500)
    target_checks: list[UnitTargetCheck] = Field(default_factory=list, max_length=40)
    hypothesis_checks: list[UnitHypothesisCheck] = Field(default_factory=list, max_length=12)
    contract_dependencies: list[UnitContractDependency] = Field(default_factory=list, max_length=60)
    unresolved_questions: list[UnitUnresolvedQuestion] = Field(default_factory=list, max_length=60)
    question_updates: list[UnitQuestionUpdate] = Field(default_factory=list, max_length=20)
    file_change_checks: list[UnitFileChangeCheck] = Field(default_factory=list, max_length=40)
    obligation_updates: list[UnitObligationUpdate] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def unique_checks(self) -> "UnitReviewRecord":
        for identifiers in (
            [item.target for item in self.target_checks],
            [item.hypothesis_id for item in self.hypothesis_checks],
            [item.question_id for item in self.question_updates],
            [item.evidence_id for item in self.file_change_checks],
            [item.obligation_id for item in self.obligation_updates],
        ):
            if len(identifiers) != len(set(identifiers)):
                raise ValueError("review record contains duplicate checks")
        return self


class UnitReviewSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["unit-review-summary-v1"] = "unit-review-summary-v1"
    status: Literal["reported", "unknown"] = "unknown"
    record: UnitReviewRecord | None = None
    last_valid_record: UnitReviewRecord | None = None
    last_valid_snapshot: dict[str, str] = Field(default_factory=dict)
    latest_attempt_status: Literal["reported", "missing", "invalid", "failed", "not_executed", "unknown"] | None = None
    latest_attempt_reason: str | None = None
    latest_attempt_snapshot: dict[str, str] = Field(default_factory=dict)
    input_protocol: Literal["canonical-evidence-v3", "legacy"] | None = None
    evidence: list[UnitEvidenceReference] = Field(default_factory=list)
    record_history: list[UnitReviewRecord] = Field(default_factory=list, max_length=3)
    reason: str = "legacy_or_missing_record"

    @model_validator(mode="after")
    def consistent_record(self) -> "UnitReviewSummary":
        if (self.status == "reported") != (self.record is not None):
            raise ValueError("reported summary requires a record; unknown summary cannot contain one")
        return self


class CrossUnitRiskReason(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: Literal[
        "changed_contract", "unresolved_dependency", "conflicting_contract",
        "dependency_coverage_gap", "unresolved_cross_unit_question",
        "relationship_unknown", "summary_unknown", "independent_changes",
        "no_cross_unit_scope",
    ]
    unit_ids: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    detail: str


class CrossUnitRelationship(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    source_unit_id: str
    target_unit_id: str
    source_file: str
    target_file: str
    type: Literal["imports", "calls", "test_of", "configures", "declared_dependency"]
    confidence: float = Field(ge=0, le=1)
    source_symbol: str | None = None
    target_symbol: str | None = None
    parser_id: str | None = None
    provenance: str = ""


class CrossUnitRiskAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["cross-unit-risk-v1"] = "cross-unit-risk-v1"
    policy_version: str = "cross-unit-policy-v1"
    decision: Literal["required", "uncertain", "skip"]
    reasons: list[CrossUnitRiskReason] = Field(default_factory=list)
    relationships: list[CrossUnitRelationship] = Field(default_factory=list)
    index_status: Literal["available", "partial", "unknown"] = "unknown"
    index_diagnostics: list[str] = Field(default_factory=list)
    execution_status: Literal["not_requested", "not_implemented", "completed", "unresolved", "failed", "cancelled"] = "not_requested"
    non_execution_reason: str | None = None


class CrossUnitFollowupRequest(BaseModel):
    """协调模型提出的只读定向补查；执行前由服务端验证。"""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=1_000)
    unit_ids: list[str] = Field(min_length=1, max_length=12)
    primary_files: list[str] = Field(min_length=1, max_length=12)
    supporting_files: list[str] = Field(default_factory=list, max_length=24)
    verification_files: list[str] = Field(default_factory=list, max_length=24)
    tracking_queries: list[str] = Field(default_factory=list, max_length=12)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    counterevidence_goal: str = Field(min_length=1, max_length=1_000)
    stop_condition: str = Field(min_length=1, max_length=1_000)
    origin_obligation_ids: list[str] = Field(default_factory=list, max_length=20)
    planning_question_indices: list[int] = Field(default_factory=list, max_length=20)

    @field_validator("primary_files", "supporting_files", "verification_files")
    @classmethod
    def validate_paths(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(validate_repo_relative_path(value) for value in values))

    @field_validator("tracking_queries")
    @classmethod
    def validate_tracking_queries(cls, values: list[str]) -> list[str]:
        from app.review.tool_scope import is_sensitive_repository_path
        result = []
        for value in values:
            name = validate_repo_relative_path(value)
            if "/" in name or any(char in name for char in "*?[]") or is_sensitive_repository_path(name):
                raise ValueError("tracking query must be a non-sensitive literal basename")
            result.append(name)
        return list(dict.fromkeys(result))


class CrossUnitRuntimeMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_calls: int = Field(default=0, ge=0)
    failed_calls: int = Field(default=0, ge=0)
    unknown_calls: int = Field(default=0, ge=0)
    cache_hits: int = Field(default=0, ge=0)
    resume_count: int = Field(default=0, ge=0)
    estimated_tokens: int = Field(default=0, ge=0)
    actual_tokens: int = Field(default=0, ge=0)
    usage_reported_calls: int = Field(default=0, ge=0)
    cost_microusd: int | None = Field(default=None, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    completed_followups: int = Field(default=0, ge=0)
    candidate_count: int = Field(default=0, ge=0)
    confirmed_count: int = Field(default=0, ge=0)


class CrossUnitCatalogBatch(BaseModel):
    """目录批次的规划覆盖；validated 不表示语义已验证或补查已完成。"""

    model_config = ConfigDict(extra="forbid")
    id: str
    input_hash: str
    input_chars: int = Field(ge=0)
    unit_ids: list[str] = Field(default_factory=list)
    relationship_ids: list[str] = Field(default_factory=list)
    risk_reason_ids: list[str] = Field(default_factory=list)
    status: Literal["pending", "validated", "skipped", "failed"] = "pending"
    reason: str | None = None


class CrossUnitPlanningQuestion(BaseModel):
    """服务端维护的问题身份、补查关联及执行后状态。"""

    model_config = ConfigDict(extra="forbid")
    id: str
    question: str
    followup_ids: list[str] = Field(default_factory=list)
    status: Literal["pending", "invalid", "unresolved", "resolved"] = "pending"
    reason: str = "尚未执行关联补查"


class CrossUnitCoordinationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["cross-unit-plan-v1"] = "cross-unit-plan-v1"
    followups: list[CrossUnitFollowupRequest] = Field(default_factory=list, max_length=3)
    unresolved_questions: list[str] = Field(default_factory=list)
    question_states: list[CrossUnitPlanningQuestion] = Field(default_factory=list)
    decision: Literal["required", "uncertain", "skip"] = "uncertain"
    reason: str = Field(default="尚未完成协调", max_length=1_000)
    relationship_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    catalog_batches: list[CrossUnitCatalogBatch] = Field(default_factory=list)
    status: Literal["proposed", "validated", "completed", "unresolved", "failed", "cancelled"] = "proposed"
    runtime_fingerprint: str | None = None
    cache_namespace: Literal["cross-unit-runtime-v1"] = "cross-unit-runtime-v1"
    runtime_metrics: CrossUnitRuntimeMetrics = Field(default_factory=CrossUnitRuntimeMetrics)
    execution_budget: ExecutionBudget = Field(default_factory=lambda: ExecutionBudget(
        max_model_calls=16, max_token_usage=120_000, max_patch_attempts=0,
    ))

    @model_validator(mode="after")
    def unique_followups(self) -> "CrossUnitCoordinationPlan":
        ids = [item.id for item in self.followups]
        if len(ids) != len(set(ids)):
            raise ValueError("followup ids must be unique")
        return self


class UnitContextOmission(BaseModel):
    """未进入诊断的检索证据范围；只保存元数据，不复制源码正文。"""

    model_config = ConfigDict(extra="forbid")
    file_path: str
    start_line: int
    end_line: int
    content_hash: str
    reason: str
    diagnosis_admission: dict[str, Any] = Field(default_factory=dict)
    supporting: bool = False  # 支持性交接证据；其未准入不降低本批次目标覆盖


class UnitInputCoverage(BaseModel):
    """诊断输入展示范围与目标范围；不表示代码正确性或候选验证状态。"""

    model_config = ConfigDict(extra="forbid")
    evidence_coverage: Literal["complete", "partial", "unknown"] = "unknown"
    target_coverage: Literal["complete", "partial", "unknown"] = "unknown"
    contract_coverage: Literal["complete", "partial", "unknown"] = "unknown"
    contract_evidence_coverage: Literal["complete", "partial", "unknown"] = "unknown"
    contract_verification_status: Literal["complete", "unresolved", "conflicting", "unknown"] = "unknown"
    unresolved_questions: list[str] = Field(default_factory=list)
    unresolved_dependencies: list[str] = Field(default_factory=list)
    blocking_dependencies: list[str] = Field(default_factory=list)
    reason: str | None = None
    omitted_components: list[str] = Field(default_factory=list)
    omitted_targets: list[str] = Field(default_factory=list)
    omitted_plan: UnitReviewPlan | None = None
    omitted_context: list[UnitContextOmission] = Field(default_factory=list)
    # 汇总缺口分别保留：运行停止、记录无效、目标未覆盖、证据交接失败互不掩盖。
    gap_reasons: list[str] = Field(default_factory=list)
    invalid_record_batches: list[str] = Field(default_factory=list)
    stopped_batches: list[str] = Field(default_factory=list)
