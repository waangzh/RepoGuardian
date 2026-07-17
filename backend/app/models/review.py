"""领域模型 —— 审查系统的所有 Pydantic 数据模型。

包含：
    - 枚举：TaskStatus, StepStatus, Severity, IssueCategory, AgentActionName
    - API 请求/响应：ReviewCreateRequest, ReviewCreateResponse
    - 领域实体：PullRequestInfo, ChangedFile, ReviewIssue, PatchResult, ...
    - 聚合根：ReviewTask（前端展示的完整状态）
"""

from datetime import datetime, timezone
from enum import Enum
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------

class TaskStatus(str, Enum):
    """审查任务生命周期状态。"""
    # pending/running are retained only so in-memory tasks created by older
    # releases can still be read while clients migrate to the granular states.
    pending = "pending"
    running = "running"
    queued = "queued"
    planning = "planning"
    reviewing = "reviewing"
    resolving_evidence = "resolving_evidence"
    verifying_issues = "verifying_issues"
    generating_patches = "generating_patches"
    validating = "validating"
    waiting_for_human = "waiting_for_human"
    completed = "completed"
    completed_with_warnings = "completed_with_warnings"
    failed = "failed"
    cancelled = "cancelled"


class ReviewMode(str, Enum):
    """产品级审查模式；默认路径永远不执行目标仓库代码。"""

    review = "review"
    review_and_suggest = "review_and_suggest"
    review_suggest_and_validate = "review_suggest_and_validate"


class ValidationBackend(str, Enum):
    """允许由 API 选择的验证后端名称，而不是任意命令或 Docker 参数。"""

    none = "none"
    local = "local"
    gvisor = "gvisor"


class ValidationStatus(str, Enum):
    not_requested = "not_requested"
    unsupported = "unsupported"
    queued = "queued"
    running = "running"
    passed = "passed"
    failed = "failed"
    infrastructure_error = "infrastructure_error"
    timed_out = "timed_out"
    inconclusive = "inconclusive"
    cancelled = "cancelled"


class ReviewPhase(str, Enum):
    """审查图的受控阶段。"""

    prepare = "prepare"
    project_detection = "project_detection"
    baseline = "baseline"
    discovery = "discovery"
    verification = "verification"
    repair = "repair"
    validation = "validation"
    publishing = "publishing"
    completed = "completed"
    failed = "failed"


class StepStatus(str, Enum):
    """单个图节点的执行状态。"""
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


class Severity(str, Enum):
    """问题严重性等级。"""
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class IssueCategory(str, Enum):
    """问题分类。"""
    correctness = "correctness"
    maintainability = "maintainability"
    performance = "performance"
    security = "security"
    test = "test"


class AgentActionName(str, Enum):
    """Agent 及兼容流程支持的操作类型。"""
    retrieve_context = "retrieve_context"
    run_static_analysis = "run_static_analysis"
    review_code = "review_code"
    generate_patch = "generate_patch"
    apply_patch = "apply_patch"
    run_tests = "run_tests"
    finish_report = "finish_report"
    request_human = "request_human"
    revise_patch = "revise_patch"
    accept_patch = "accept_patch"
    abandon_patch = "abandon_patch"


class CommandId(str, Enum):
    """服务端注册的逻辑命令标识，不能由模型扩展为任意 Shell 文本。"""

    python_static_default = "python.static.default"
    python_test_collect = "python.test.collect"
    python_test_targeted = "python.test.targeted"
    python_test_full = "python.test.full"


class ValidationStage(str, Enum):
    """同一工作树在补丁前后的三个验证阶段。"""

    base = "base"
    head = "head"
    patched = "patched"


class PatchStatus(str, Enum):
    """候选补丁在独立验证生命周期中的受验证状态。"""

    suggested = "suggested"
    unverified = "unverified"
    validation_pending = "validation_pending"
    verified = "verified"
    validation_failed = "validation_failed"
    validation_inconclusive = "validation_inconclusive"
    abandoned = "abandoned"
    superseded = "superseded"



class RetrievalRelevanceType(str, Enum):
    """服务端支持的、可审计的上下文关联类型。"""

    direct = "direct"
    caller = "caller"
    callee = "callee"
    test = "test"
    module_config = "module_config"
    text = "text"
    adjacent = "adjacent"
    type_definition = "type_definition"
    import_source = "import_source"
    failure_location = "failure_location"


class FixRisk(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


def _validate_repo_relative_path(value: str) -> str:
    """只接受与仓库索引格式一致的 POSIX 相对路径。"""
    if not isinstance(value, str) or not value or len(value) > 260:
        raise ValueError("repository path must be a non-empty relative path")
    if "\\" in value or "\x00" in value or value.startswith(("/", "~")) or ":" in value:
        raise ValueError("repository path must use a safe POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("repository path traversal is not allowed")
    if path.as_posix() != value:
        raise ValueError("repository path must be normalized")
    return value


class ContextRetrievalPlan(BaseModel):
    """模型提出、服务端归一化并按索引执行的上下文检索计划。"""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)
    target_files: list[str] = Field(default_factory=list, max_length=12)
    target_symbols: list[str] = Field(default_factory=list, max_length=12)
    search_terms: list[str] = Field(default_factory=list, max_length=8)
    relevance_types: list[RetrievalRelevanceType] = Field(min_length=1, max_length=10)
    include_callers: bool = False
    include_callees: bool = False
    include_tests: bool = False
    max_results: int = Field(default=12, ge=1, le=20)
    depth: int = Field(default=1, ge=0, le=2)

    @field_validator("target_files")
    @classmethod
    def validate_target_files(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(_validate_repo_relative_path(value) for value in values))

    @field_validator("target_symbols")
    @classmethod
    def validate_target_symbols(cls, values: list[str]) -> list[str]:
        import re

        symbol_pattern = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$")
        if any(not symbol_pattern.fullmatch(value) for value in values):
            raise ValueError("target symbols must be indexed symbol names")
        return list(dict.fromkeys(values))

    @field_validator("search_terms")
    @classmethod
    def validate_search_terms(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            if not isinstance(value, str) or not value.strip() or len(value) > 120:
                raise ValueError("search terms must be short non-empty literals")
            if any(ord(char) < 32 for char in value):
                raise ValueError("search terms cannot contain control characters")
            cleaned.append(value.strip())
        return list(dict.fromkeys(cleaned))

    @field_validator("max_results", mode="before")
    @classmethod
    def clamp_max_results(cls, value: Any) -> int:
        if isinstance(value, bool):
            raise ValueError("max_results must be an integer")
        value = int(value)
        if value < 1:
            raise ValueError("max_results must be positive")
        return min(value, 20)

    @field_validator("depth", mode="before")
    @classmethod
    def clamp_depth(cls, value: Any) -> int:
        if isinstance(value, bool):
            raise ValueError("depth must be an integer")
        value = int(value)
        if value < 0:
            raise ValueError("depth must not be negative")
        return min(value, 2)

    @model_validator(mode="after")
    def require_a_safe_target(self) -> "ContextRetrievalPlan":
        if not (self.target_files or self.target_symbols or self.search_terms):
            raise ValueError("retrieval plan requires a file, symbol, or literal search term")
        return self


class HumanReviewRequest(BaseModel):
    """必须人工确认时向调用方暴露的最小结构化信息。"""

    model_config = ConfigDict(extra="forbid")

    missing_information: list[str] = Field(min_length=1, max_length=8)
    known_evidence: list[str] = Field(min_length=1, max_length=12)
    questions: list[str] = Field(min_length=1, max_length=8)
    prohibited_operations: list[str] = Field(min_length=1, max_length=8)

    @field_validator("missing_information", "known_evidence", "questions", "prohibited_operations")
    @classmethod
    def validate_items(cls, values: list[str]) -> list[str]:
        if any(not isinstance(value, str) or not value.strip() or len(value) > 500 for value in values):
            raise ValueError("human review fields must contain short non-empty text")
        return list(dict.fromkeys(value.strip() for value in values))


class FailureKind(str, Enum):
    """验证失败的受控分类。"""

    dependency_missing = "dependency_missing"
    test_collection_error = "test_collection_error"
    timeout = "timeout"
    infrastructure = "infrastructure"
    code_regression = "code_regression"
    unknown = "unknown"


class ExecutionBudget(BaseModel):
    """限制一次审查中可消耗的外部与模型资源。"""

    context_retrievals: int = Field(default=0, ge=0)
    max_context_retrievals: int = Field(default=2, ge=0)
    diagnosis_attempts: int = Field(default=0, ge=0)
    max_diagnosis_attempts: int = Field(default=1, ge=0)
    patch_attempts: int = Field(default=0, ge=0)
    max_patch_attempts: int = Field(default=3, ge=0)
    model_calls: int = Field(default=0, ge=0)
    max_model_calls: int = Field(default=6, ge=0)
    token_usage: int = Field(default=0, ge=0)
    max_token_usage: int = Field(default=24_000, ge=0)

    def can_consume(self, **amounts: int) -> bool:
        """检查一组预算消耗是否仍在上限内。"""
        for name, amount in amounts.items():
            if amount < 0:
                raise ValueError("budget consumption must not be negative")
            limit_name = f"max_{name}"
            if not hasattr(self, name) or not hasattr(self, limit_name):
                raise ValueError(f"unsupported budget metric: {name}")
            if getattr(self, name) + amount > getattr(self, limit_name):
                return False
        return True

    def consume(self, **amounts: int) -> "ExecutionBudget":
        """返回已消耗预算的新实例；超限时拒绝执行。"""
        if not self.can_consume(**amounts):
            raise ValueError("execution budget exhausted")
        return self.model_copy(
            update={name: getattr(self, name) + amount for name, amount in amounts.items()}
        )


# ---------------------------------------------------------------------------
# API 请求/响应
# ---------------------------------------------------------------------------

def _default_review_mode() -> ReviewMode:
    """延迟读取配置，避免领域模型与 Settings 在模块导入时互相依赖。"""
    from app.core.config import settings

    return ReviewMode(settings.repoguardian_default_review_mode)


def _default_validation_backend() -> ValidationBackend:
    from app.core.config import settings

    return ValidationBackend(settings.repoguardian_default_validation_backend)


class ReviewCreateRequest(BaseModel):
    """POST /api/reviews 请求体。"""
    pr_url: HttpUrl
    model: str | None = None
    mode: ReviewMode = Field(default_factory=_default_review_mode)
    generate_patches: bool = False
    validation_backend: ValidationBackend = Field(default_factory=_default_validation_backend)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_product_policy(self) -> "ReviewCreateRequest":
        if self.mode == ReviewMode.review:
            if self.generate_patches:
                raise ValueError("mode=review does not allow generate_patches=true")
            if self.validation_backend != ValidationBackend.none:
                raise ValueError("mode=review requires validation_backend=none")
        elif self.mode == ReviewMode.review_and_suggest:
            if self.validation_backend != ValidationBackend.none:
                raise ValueError("mode=review_and_suggest requires validation_backend=none")
        return self


class ReviewCreateResponse(BaseModel):
    """POST /api/reviews 响应体。"""
    task_id: str
    status: TaskStatus


class TaskStep(BaseModel):
    """图节点执行步骤记录。"""
    name: str
    status: StepStatus = StepStatus.pending
    message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


# ---------------------------------------------------------------------------
# PR 相关
# ---------------------------------------------------------------------------

class PullRequestRef(BaseModel):
    """PR 的 base 或 head 分支引用。"""
    ref: str
    sha: str
    repo_clone_url: str


class PullRequestInfo(BaseModel):
    """从 GitHub API 拉取的 PR 元数据。"""
    owner: str
    repo: str
    number: int
    title: str
    html_url: str
    clone_url: str
    base: PullRequestRef
    head: PullRequestRef


# ---------------------------------------------------------------------------
# Diff 解析产物
# ---------------------------------------------------------------------------

class ChangedLine(BaseModel):
    """diff 中的一行变更。"""
    line_no: int | None
    content: str


class DiffHunk(BaseModel):
    """diff 中的一个 hunk（连续变更块）。"""
    old_start: int
    old_length: int
    new_start: int
    new_length: int
    added_lines: list[ChangedLine] = Field(default_factory=list)
    removed_lines: list[ChangedLine] = Field(default_factory=list)


class ChangedFile(BaseModel):
    """一个文件的 diff 解析结果。"""
    file_path: str
    change_type: str   # added / modified / deleted
    additions: int
    deletions: int
    hunks: list[DiffHunk] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 审查产物
# ---------------------------------------------------------------------------

class ReviewIssue(BaseModel):
    """LLM 审查发现的一个代码问题。"""
    id: str = Field(default_factory=lambda: uuid4().hex)
    file_path: str
    line_no: int | None = None
    severity: Severity
    category: IssueCategory
    title: str
    description: str
    suggestion: str
    confidence: float = Field(ge=0, le=1)   # 0-1，LLM 置信度
    auto_fixable: bool = False               # 是否可自动修复
    evidence: str = Field(min_length=3, max_length=2_000)
    evidence_locations: list["EvidenceLocation"] = Field(min_length=1, max_length=12)
    affected_behavior: str = Field(min_length=3, max_length=1_000)
    assumptions: list[str] = Field(default_factory=list, max_length=8)
    related_test_ids: list[str] = Field(default_factory=list, max_length=12)
    fix_risk: FixRisk = FixRisk.high
    requires_human_confirmation: bool = False

    @field_validator("file_path")
    @classmethod
    def validate_file_path(cls, value: str) -> str:
        return _validate_repo_relative_path(value)

    @field_validator("line_no")
    @classmethod
    def validate_line_no(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("line_no must be positive")
        return value

    @field_validator("assumptions", "related_test_ids")
    @classmethod
    def validate_short_text_lists(cls, values: list[str]) -> list[str]:
        if any(not isinstance(value, str) or not value.strip() or len(value) > 500 for value in values):
            raise ValueError("issue text lists must contain short non-empty values")
        return list(dict.fromkeys(value.strip() for value in values))

    @model_validator(mode="after")
    def restrict_auto_fix_to_low_risk_evidence(self) -> "ReviewIssue":
        if self.auto_fixable and (
            self.fix_risk != FixRisk.low or self.requires_human_confirmation
        ):
            raise ValueError("only low-risk issues without human confirmation are auto-fixable")
        return self


class EvidenceLocation(BaseModel):
    """问题证据在当前 Head 工作树中的精确位置。"""

    model_config = ConfigDict(extra="forbid")

    file_path: str
    line_no: int = Field(ge=1)

    @field_validator("file_path")
    @classmethod
    def validate_file_path(cls, value: str) -> str:
        return _validate_repo_relative_path(value)


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
        elif self.action == AgentActionName.apply_patch:
            patch_id = self.tool_args.get("patch_id")
            if set(self.tool_args) != {"patch_id"} or not isinstance(patch_id, str) or not patch_id:
                raise ValueError("apply_patch requires a server-selected patch_id only")
        elif self.tool_args:
            raise ValueError(f"tool_args are not allowed for action '{self.action.value}'")

        if self.action == AgentActionName.request_human:
            if self.human_request is None:
                raise ValueError("request_human requires a structured human_request")
        elif self.human_request is not None:
            raise ValueError("human_request is only allowed for request_human")
        return self


class AgentEvent(BaseModel):
    """Agent 决策事件日志条目。"""
    action: AgentActionName | str
    reason: str
    status: str         # selected / completed / failed
    message: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# 工具执行产物
# ---------------------------------------------------------------------------

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


class ValidationSnapshot(BaseModel):
    """Base、Head 或 Patched 阶段的一组受控验证结果。"""

    id: str = Field(default_factory=lambda: uuid4().hex)
    stage: ValidationStage
    sha: str = Field(min_length=1)
    patch_id: str | None = None
    command_results: list[TestRunResult] = Field(default_factory=list)
    collected_test_count: int | None = Field(default=None, ge=0)
    failure_fingerprints: list[FailureFingerprint] = Field(default_factory=list)
    passed: bool
    failure_kind: FailureKind | None = None
    failure_detail: str | None = None


class ValidationDelta(BaseModel):
    """两个验证快照的语义差异，用于区分既有失败与新增回归。"""

    from_stage: ValidationStage
    to_stage: ValidationStage
    patch_id: str | None = None
    previous_passed: bool
    current_passed: bool
    failure_kind: FailureKind | None = None
    introduced_failure: bool = False
    resolved_failure: bool = False
    introduced_failures: list[FailureFingerprint] = Field(default_factory=list)
    resolved_failures: list[FailureFingerprint] = Field(default_factory=list)


class ValidationResult(BaseModel):
    """面向 API 的验证结论，与旧的 Base/Head 快照解耦。"""

    id: str = Field(default_factory=lambda: uuid4().hex)
    patch_id: str | None = None
    backend: ValidationBackend = ValidationBackend.none
    status: ValidationStatus
    detail: str | None = None
    snapshot_id: str | None = None


class PatchResult(BaseModel):
    """一个自动修复 patch。"""
    id: str = Field(default_factory=lambda: uuid4().hex)
    issue_id: str | None = None          # 关联的 ReviewIssue ID
    diff_content: str                    # unified diff 内容
    status: PatchStatus = PatchStatus.unverified
    revision_of: str | None = None
    attempt_number: int = Field(default=1, ge=1)
    validation_snapshot_id: str | None = None
    validation_backend: ValidationBackend | None = None
    validation_result_id: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("status", mode="before")
    @classmethod
    def migrate_legacy_status(cls, value: PatchStatus | str) -> PatchStatus | str:
        """读取旧任务时归一化旧补丁状态，避免继续向 API 暴露混合语义。"""
        legacy_statuses = {
            "generated": PatchStatus.suggested,
            "applied": PatchStatus.validation_pending,
            "apply_failed": PatchStatus.abandoned,
            "validation_passed": PatchStatus.verified,
        }
        return legacy_statuses.get(value, value)


class ContextSnippet(BaseModel):
    """代码上下文片段（CodeSearch 检索产物）。"""
    file: str
    start_line: int
    end_line: int
    content: str
    relevance: str         # direct / caller / test / adjacent
    symbol: str | None = None


class RepoSnapshot(BaseModel):
    """仓库概览快照（RepoIndexer 产出）。"""
    language: str
    framework: str | None = None
    test_framework: str | None = None
    total_files: int


class ReviewSummary(BaseModel):
    """API 中与 issues/patches/validation 并列的只读审查结果摘要。"""

    mode: ReviewMode = ReviewMode.review
    status: TaskStatus = TaskStatus.queued
    completed: bool = False


# ---------------------------------------------------------------------------
# 聚合根
# ---------------------------------------------------------------------------

class ReviewTask(BaseModel):
    """审查任务聚合根，聚合所有阶段的产出，供前端完整展示。"""
    id: str
    status: TaskStatus = TaskStatus.queued
    phase: ReviewPhase = ReviewPhase.prepare
    pr_url: str
    model: str | None = None
    mode: ReviewMode = ReviewMode.review
    generate_patches: bool = False
    validation_backend: ValidationBackend = ValidationBackend.none
    review: ReviewSummary = Field(default_factory=ReviewSummary)
    steps: list[TaskStep] = Field(default_factory=list)
    pr: PullRequestInfo | None = None
    changed_files: list[ChangedFile] = Field(default_factory=list)
    issues: list[ReviewIssue] = Field(default_factory=list)
    context_snippets: list[ContextSnippet] = Field(default_factory=list)
    repo_snapshot: RepoSnapshot | None = None
    project_profile: ProjectProfile | None = None
    static_results: list[TestRunResult] = Field(default_factory=list)
    validation_snapshots: list[ValidationSnapshot] = Field(default_factory=list)
    validation_deltas: list[ValidationDelta] = Field(default_factory=list)
    validation: list[ValidationResult] = Field(default_factory=list)
    patches: list[PatchResult] = Field(default_factory=list)
    test_results: list[TestRunResult] = Field(default_factory=list)
    agent_events: list[AgentEvent] = Field(default_factory=list)
    human_request: HumanReviewRequest | None = None
    report_markdown: str | None = None
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

