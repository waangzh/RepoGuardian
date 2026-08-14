"""领域模型 —— 审查系统的所有 Pydantic 数据模型。

包含：
    - 枚举：TaskStatus, StepStatus, Severity, IssueCategory, AgentActionName
    - API 请求/响应：ReviewCreateRequest, ReviewCreateResponse
    - 领域实体：PullRequestInfo, ChangedFile, ReviewIssue, PatchResult, ...
    - 聚合根：ReviewTask（前端展示的完整状态）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Generic, Iterator, Literal, TypeVar
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    computed_field,
    field_validator,
    model_validator,
)


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
    user_runner = "user_runner"
    project_ci = "project_ci"
    gvisor = "gvisor"


class ValidationStatus(str, Enum):
    passed = "passed"
    failed = "failed"
    unsupported = "unsupported"
    infrastructure_error = "infrastructure_error"
    timed_out = "timed_out"
    cancelled = "cancelled"
    inconclusive = "inconclusive"


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


class ReviewUnitComplexity(str, Enum):
    small = "small"
    medium = "medium"
    large = "large"


class ReviewUnitStatus(str, Enum):
    pending = "pending"
    planning = "planning"
    reviewing = "reviewing"
    completed = "completed"
    failed = "failed"
    timed_out = "timed_out"
    cancelled = "cancelled"
    needs_human = "needs_human"


class UnitPlanStatus(str, Enum):
    planned = "planned"
    skipped = "skipped"
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


class IssueStatus(str, Enum):
    """问题从模型候选到发布前的服务端状态。"""

    candidate = "candidate"
    evidence_resolved = "evidence_resolved"
    confirmed = "confirmed"
    dismissed = "dismissed"
    needs_human = "needs_human"
    published = "published"


class IssueVerificationDecision(str, Enum):
    """独立 verifier 只能缩小候选集合，不能创建新的问题。"""

    keep = "keep"
    drop = "drop"
    needs_human = "needs_human"


class EvidenceResolutionMethod(str, Enum):
    diff_exact = "diff_exact"
    diff_normalized = "diff_normalized"
    file_exact = "file_exact"
    symbol_assisted = "symbol_assisted"
    unresolved = "unresolved"


class CommentPlacement(str, Enum):
    inline = "inline"
    summary = "summary"
    suppressed = "suppressed"
    needs_human = "needs_human"


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
    report_issue = "report_issue"
    task_done = "task_done"


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


class PatchApplyCheckStatus(str, Enum):
    """候选补丁的确定性可应用性检查状态，不代表功能验证。"""

    not_checked = "not_checked"
    passed = "passed"
    failed = "failed"



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


class ModelUsage(BaseModel):
    """一次模型调用的只读资源观测；不参与当前逻辑预算判定。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex)
    provider: str
    model: str
    operation: str
    review_unit_id: str | None = None
    unit_complexity: "ReviewUnitComplexity | None" = None
    accounted_tokens_estimate: int | None = Field(default=None, ge=0)
    estimated_input_tokens: int = Field(default=0, ge=0)
    max_output_tokens: int = Field(default=0, ge=0)
    actual_input_tokens: int | None = Field(default=None, ge=0)
    actual_output_tokens: int | None = Field(default=None, ge=0)
    actual_total_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    reasoning_output_tokens: int | None = Field(default=None, ge=0)
    latency_ms: int = Field(ge=0)
    cost_microusd: int | None = Field(default=None, ge=0)
    usage_available: bool = False
    accounting_source: Literal["actual", "missing"] = "missing"
    response_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @computed_field
    @property
    def estimation_delta_tokens(self) -> int | None:
        if self.accounted_tokens_estimate is None or self.actual_total_tokens is None:
            return None
        return self.accounted_tokens_estimate - self.actual_total_tokens


class ModelUsageStats(BaseModel):
    calls: int = Field(default=0, ge=0)
    usage_available_calls: int = Field(default=0, ge=0)
    usage_missing_calls: int = Field(default=0, ge=0)
    usage_coverage_rate: float = Field(default=0.0, ge=0, le=1)
    actual_input_tokens: int = Field(default=0, ge=0)
    actual_output_tokens: int = Field(default=0, ge=0)
    actual_total_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    reasoning_output_tokens: int = Field(default=0, ge=0)
    accounted_tokens_estimate: int = Field(default=0, ge=0)
    estimation_delta_tokens: int = 0
    cost_microusd: int = Field(default=0, ge=0)
    cost_available_calls: int = Field(default=0, ge=0)
    input_tokens_p50: int | None = Field(default=None, ge=0)
    input_tokens_p95: int | None = Field(default=None, ge=0)
    output_tokens_p50: int | None = Field(default=None, ge=0)
    output_tokens_p95: int | None = Field(default=None, ge=0)
    latency_ms_p50: int | None = Field(default=None, ge=0)
    latency_ms_p95: int | None = Field(default=None, ge=0)


class ModelUsageGroup(BaseModel):
    key: str
    stats: ModelUsageStats


class ModelUsageSummary(BaseModel):
    overall: ModelUsageStats = Field(default_factory=ModelUsageStats)
    by_operation: list[ModelUsageGroup] = Field(default_factory=list)
    by_unit_complexity: list[ModelUsageGroup] = Field(default_factory=list)
    by_provider: list[ModelUsageGroup] = Field(default_factory=list)


ModelCallValue = TypeVar("ModelCallValue")


@dataclass(frozen=True)
class ModelCallResult(Generic[ModelCallValue]):
    """Provider 业务结果及其对应的资源观测。"""

    value: ModelCallValue
    usage: ModelUsage

    def __getattr__(self, name: str) -> Any:
        return getattr(self.value, name)

    def __iter__(self) -> Iterator[Any]:
        return iter(self.value)  # type: ignore[arg-type]

    def __len__(self) -> int:
        return len(self.value)  # type: ignore[arg-type]

    def __getitem__(self, key: Any) -> Any:
        return self.value[key]  # type: ignore[index]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ModelCallResult):
            return self.value == other.value and self.usage == other.usage
        return self.value == other


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


def _default_validation_profile() -> str:
    from app.core.config import settings

    return settings.repoguardian_default_validation_profile


class ReviewCreateRequest(BaseModel):
    """POST /api/reviews 请求体。"""
    pr_url: HttpUrl
    model: str | None = None
    mode: ReviewMode = Field(default_factory=_default_review_mode)
    generate_patches: bool = False
    validation_backend: ValidationBackend = Field(default_factory=_default_validation_backend)
    validation_profile: str = Field(default_factory=_default_validation_profile)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_product_policy(self) -> "ReviewCreateRequest":
        if self.mode == ReviewMode.review:
            if self.generate_patches:
                raise ValueError("mode=review does not allow generate_patches=true")
            self.validation_backend = ValidationBackend.none
        elif self.mode == ReviewMode.review_and_suggest:
            self.validation_backend = ValidationBackend.none
        elif self.validation_backend == ValidationBackend.user_runner:
            from app.core.config import registered_runner_profiles

            if self.validation_profile not in registered_runner_profiles():
                raise ValueError("validation_profile is not registered by server policy")
        return self


class ReviewCreateResponse(BaseModel):
    """POST /api/reviews 响应体。"""
    task_id: str
    status: TaskStatus


class ReviewPreviewRequest(BaseModel):
    """仅执行确定性 PR 分析，不创建任务或调用模型。"""

    pr_url: HttpUrl
    mode: ReviewMode = Field(default_factory=_default_review_mode)
    generate_patches: bool = False
    validation_backend: ValidationBackend = Field(default_factory=_default_validation_backend)
    validation_profile: str = Field(default_factory=_default_validation_profile)
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_product_policy(self) -> "ReviewPreviewRequest":
        normalized = ReviewCreateRequest(
            pr_url=self.pr_url,
            mode=self.mode,
            generate_patches=self.generate_patches,
            validation_backend=self.validation_backend,
            validation_profile=self.validation_profile,
        )
        self.validation_backend = normalized.validation_backend
        self.validation_profile = normalized.validation_profile
        return self


class TaskStep(BaseModel):
    """图节点执行步骤记录。"""
    name: str
    status: StepStatus = StepStatus.pending
    message: str | None = None
    progress: "TaskStepProgress | None" = None
    started_at: datetime | None = None
    updated_at: datetime | None = None
    finished_at: datetime | None = None


class TaskStepProgress(BaseModel):
    """长时间运行步骤的结构化局部进度；percent 为空时表示不可确定进度。"""

    phase: str
    operation: str | None = None
    percent: int | None = Field(default=None, ge=0, le=100)
    current: int | None = Field(default=None, ge=0)
    total: int | None = Field(default=None, ge=0)
    detail: str | None = Field(default=None, max_length=200)


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
    body: str | None = Field(default=None, max_length=65_536)
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


class DiffLine(BaseModel):
    """hunk 中按原始顺序保存的双侧行。"""

    kind: Literal["added", "context", "deleted"]
    content: str
    old_line_no: int | None = None
    new_line_no: int | None = None


class DiffHunk(BaseModel):
    """diff 中的一个 hunk（连续变更块）。"""
    old_start: int
    old_length: int
    new_start: int
    new_length: int
    hunk_id: str = ""
    lines: list[DiffLine] = Field(default_factory=list)
    added_lines: list[ChangedLine] = Field(default_factory=list)
    removed_lines: list[ChangedLine] = Field(default_factory=list)


class ChangedFile(BaseModel):
    """一个文件的 diff 解析结果。"""
    file_path: str
    old_file_path: str | None = None
    change_type: str   # added / modified / deleted
    additions: int
    deletions: int
    is_binary: bool = False
    hunks: list[DiffHunk] = Field(default_factory=list)


class PlannedChangedFile(BaseModel):
    """Planner 对变更文件作出的确定性分类和纳入结论。"""

    file_path: str
    old_file_path: str | None = None
    change_type: str
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    classifications: list[str] = Field(default_factory=list)
    included: bool
    excluded_reason: str | None = None


class ExcludedReviewFile(BaseModel):
    file_path: str
    reason: str
    classifications: list[str] = Field(default_factory=list)


class ContextProvenance(BaseModel):
    """Planner 为文件进入 Unit 可读范围给出的可审计依据。"""

    file: str
    source: str
    distance: int = Field(ge=0, le=2)
    confidence: float = Field(ge=0, le=1)
    why_retrieved: str
    unit_id: str | None = None


class ReviewUnit(BaseModel):
    """由确定性 Planner 生成的最小独立审查单元。"""

    id: str
    primary_files: list[str] = Field(min_length=1)
    related_files: list[str] = Field(default_factory=list)
    context_provenance: list[ContextProvenance] = Field(default_factory=list)
    diff_hunk_ids: list[str] = Field(default_factory=list)
    changed_symbols: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    risk_tags: list[str] = Field(default_factory=list)
    estimated_tokens: int = Field(ge=0)
    complexity: ReviewUnitComplexity
    fingerprint: str
    grouping_reason: str

    @model_validator(mode="after")
    def validate_file_roles(self) -> "ReviewUnit":
        self.primary_files = list(dict.fromkeys(self.primary_files))
        primary = set(self.primary_files)
        self.related_files = [
            path for path in dict.fromkeys(self.related_files) if path not in primary
        ]
        readable = primary | set(self.related_files)
        self.context_provenance = [
            item for item in self.context_provenance if item.file in readable
        ]
        return self


class ReviewToolScope(BaseModel):
    """单个 Unit 的不可扩张工具权限。"""

    review_unit_id: str
    commentable_files: set[str]
    readable_files: set[str]
    context_provenance: dict[str, ContextProvenance] = Field(default_factory=dict)
    repository_root: str | None = None
    max_lines_per_read: int = Field(gt=0)
    max_search_results: int = Field(gt=0)

    @model_validator(mode="after")
    def require_commentable_subset(self) -> "ReviewToolScope":
        if not self.commentable_files <= self.readable_files:
            raise ValueError("commentable_files must be a subset of readable_files")
        if not set(self.context_provenance) <= self.readable_files:
            raise ValueError("context provenance files must be readable")
        return self


class ReviewPlan(BaseModel):
    planner_version: str
    changed_files: list[PlannedChangedFile] = Field(default_factory=list)
    review_units: list[ReviewUnit] = Field(default_factory=list)
    excluded_files: list[ExcludedReviewFile] = Field(default_factory=list)
    matched_rules: list[str] = Field(default_factory=list)
    risk_tags: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ReviewPreviewResponse(BaseModel):
    mode: ReviewMode
    changed_file_count: int = Field(ge=0)
    included_file_count: int = Field(ge=0)
    changed_files: list[PlannedChangedFile] = Field(default_factory=list)
    review_units: list[ReviewUnit] = Field(default_factory=list)
    excluded_files: list[ExcludedReviewFile] = Field(default_factory=list)
    matched_rules: list[str] = Field(default_factory=list)
    risk_tags: list[str] = Field(default_factory=list)
    planning_model_calls: int = Field(ge=0)
    estimated_model_calls: int = Field(ge=0)
    max_model_calls: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
    patch_generation_enabled: bool
    validation_backend: "ValidationBackendPreview"
    warnings: list[str] = Field(default_factory=list)


class ValidationBackendPreview(BaseModel):
    name: ValidationBackend
    available: bool
    unavailable_reason: str | None = None


# ---------------------------------------------------------------------------
# 审查产物
# ---------------------------------------------------------------------------

class EvidenceCandidate(BaseModel):
    """未唯一定位时保留的只读候选位置，便于调试和人工确认。"""

    model_config = ConfigDict(extra="forbid")

    file_path: str
    side: Literal["head", "base"]
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    hunk_id: str | None = None


class EvidenceAnchor(BaseModel):
    """模型提供代码片段，位置与哈希只能由服务端解析写入。"""

    model_config = ConfigDict(extra="forbid")

    file_path: str
    existing_code: str = Field(min_length=1, max_length=8_000)
    symbol: str | None = None
    expected_side: Literal["head", "base", "either"] = "head"
    expected_hunk_id: str | None = None
    context_before: list[str] = Field(default_factory=list, max_length=12)
    context_after: list[str] = Field(default_factory=list, max_length=12)
    resolved_start_line: int | None = Field(default=None, ge=1)
    resolved_end_line: int | None = Field(default=None, ge=1)
    resolution_method: EvidenceResolutionMethod = EvidenceResolutionMethod.unresolved
    match_count: int = Field(default=0, ge=0)
    anchor_hash: str | None = None
    resolved_side: Literal["head", "base"] | None = None
    candidate_locations: list[EvidenceCandidate] = Field(default_factory=list)
    unresolved_reason: str | None = None

    @field_validator("file_path")
    @classmethod
    def validate_file_path(cls, value: str) -> str:
        return _validate_repo_relative_path(value)

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


class EvidenceAnchorInput(BaseModel):
    """LLM 可填写的证据字段白名单。"""

    model_config = ConfigDict(extra="forbid")

    file_path: str
    existing_code: str = Field(min_length=1, max_length=8_000)
    symbol: str | None = None
    expected_side: Literal["head", "base", "either"] = "head"
    expected_hunk_id: str | None = None
    context_before: list[str] = Field(default_factory=list, max_length=12)
    context_after: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("file_path")
    @classmethod
    def validate_file_path(cls, value: str) -> str:
        return _validate_repo_relative_path(value)


class ReviewIssueInput(BaseModel):
    """严格的模型输出结构；行号仅兼容读取且不会进入领域模型。"""

    model_config = ConfigDict(extra="forbid")

    title: str
    category: IssueCategory
    severity: Severity
    confidence: float = Field(ge=0, le=1)
    affected_behavior: str = Field(min_length=3, max_length=1_000)
    failure_scenario: str = Field(min_length=3, max_length=2_000)
    recommendation: str = Field(min_length=1, max_length=2_000)
    primary_evidence: EvidenceAnchorInput
    supporting_evidence: list[EvidenceAnchorInput] = Field(default_factory=list, max_length=12)
    assumptions: list[str] = Field(default_factory=list, max_length=8)
    related_tests: list[str] = Field(default_factory=list, max_length=12)
    requires_human_confirmation: bool = False
    auto_fix_eligible: bool = False
    fix_risk: FixRisk = FixRisk.low
    line_number: int | None = Field(default=None, ge=1, deprecated=True)
    line_no: int | None = Field(default=None, ge=1, deprecated=True)

    def to_issue(self, review_unit_id: str = "unassigned") -> "ReviewIssue":
        def anchor(value: EvidenceAnchorInput) -> EvidenceAnchor:
            return EvidenceAnchor(
                **value.model_dump(),
                resolution_method=EvidenceResolutionMethod.unresolved,
            )

        return ReviewIssue(
            review_unit_id=review_unit_id,
            title=self.title,
            category=self.category,
            severity=self.severity,
            confidence=self.confidence,
            affected_behavior=self.affected_behavior,
            failure_scenario=self.failure_scenario,
            recommendation=self.recommendation,
            primary_evidence=anchor(self.primary_evidence),
            supporting_evidence=[anchor(item) for item in self.supporting_evidence],
            assumptions=self.assumptions,
            related_tests=self.related_tests,
            requires_human_confirmation=self.requires_human_confirmation,
            auto_fix_eligible=self.auto_fix_eligible,
            fix_risk=self.fix_risk,
            status=IssueStatus.candidate,
        )


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
    source_review_unit_ids: list[str] = Field(default_factory=list)
    source_issue_ids: list[str] = Field(default_factory=list)

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


class IssueVerification(BaseModel):
    """Verifier 的唯一合法输出；不包含新 Issue 或 primary evidence 修改。"""

    model_config = ConfigDict(extra="forbid")

    issue_id: str
    decision: IssueVerificationDecision
    reason: str = Field(min_length=1, max_length=2_000)
    contradicting_evidence: list[EvidenceAnchor] = Field(default_factory=list, max_length=12)
    adjusted_severity: Severity | None = None


class IssueDeduplicationDecision(BaseModel):
    """候选重复组内的受限语义合并结论。"""

    model_config = ConfigDict(extra="forbid")

    canonical_issue_id: str
    duplicate_issue_ids: list[str] = Field(default_factory=list)
    merged_rationale: str = Field(min_length=1, max_length=2_000)


class IssueMetrics(BaseModel):
    """Issue 筛选、验证和聚合阶段的任务级指标。"""

    model_config = ConfigDict(extra="forbid")

    candidate_issue_count: int = Field(default=0, ge=0)
    deterministic_drop_count: int = Field(default=0, ge=0)
    verifier_drop_count: int = Field(default=0, ge=0)
    needs_human_count: int = Field(default=0, ge=0)
    duplicate_count: int = Field(default=0, ge=0)
    confirmed_count: int = Field(default=0, ge=0)
    severity_adjustment_count: int = Field(default=0, ge=0)
    verifier_call_count: int = Field(default=0, ge=0)
    verifier_token_count: int = Field(default=0, ge=0)


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
        return list(dict.fromkeys(_validate_repo_relative_path(value) for value in values))


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


class ValidationCheck(BaseModel):
    """验证后端报告的单项结构化检查。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    status: ValidationStatus
    detail: str | None = None


class ValidationCapabilities(BaseModel):
    """由服务端声明的验证后端安全能力。"""

    model_config = ConfigDict(extra="forbid")

    available: bool
    supported_languages: list[str] = Field(default_factory=list)
    supported_profiles: list[str] = Field(default_factory=list)
    executes_untrusted_code: bool
    requires_user_configuration: bool
    unavailable_reason: str | None = None


class PatchValidationRequest(BaseModel):
    """传给服务端所选验证后端的不可变标识。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    patch_id: str
    repository_id: str
    base_sha: str
    head_sha: str
    patch_sha: str
    validation_profile: str | None = None
    repository_clone_url: str | None = None
    repository_fetch_ref: str | None = None
    patch_content: str | None = None
    is_fork: bool = False


class PatchValidationResult(BaseModel):
    """后端无关的补丁验证结果，也是 API 的公开结构。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex)
    backend: str = Field(min_length=1)
    status: ValidationStatus
    head_sha: str
    patch_sha: str
    checks: list[ValidationCheck] = Field(default_factory=list)
    resolved_failures: list[str] = Field(default_factory=list)
    new_failures: list[str] = Field(default_factory=list)
    environment_fingerprint: str | None = None
    trusted: bool
    trust_source: str | None = None
    runner_id: str | None = None
    validation_request_id: str | None = None
    profile: str | None = None
    exit_status: int | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    log_summary: str | None = None
    artifact_references: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    completed_at: datetime | None = None


class ValidationResult(PatchValidationResult):
    """保留给 ReviewTask.validation 的兼容名称。"""

    patch_id: str | None = None


class PatchEligibilityDecision(BaseModel):
    """服务端对单个 confirmed Issue 作出的补丁生成准入结论。"""

    model_config = ConfigDict(extra="forbid")

    issue_id: str
    eligible: bool
    reasons: list[str] = Field(default_factory=list)
    allowed_files: list[str] = Field(default_factory=list)
    max_files: int = Field(ge=1)
    max_changed_lines: int = Field(ge=1)

    @field_validator("allowed_files")
    @classmethod
    def validate_allowed_files(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(_validate_repo_relative_path(value) for value in values))


class PatchApplyCheck(BaseModel):
    """只描述 git apply 与隔离恢复结果，绝不表达功能正确性。"""

    model_config = ConfigDict(extra="forbid")

    status: PatchApplyCheckStatus = PatchApplyCheckStatus.not_checked
    detail: str = "尚未执行可应用性检查。"
    checked_head_sha: str | None = None
    worktree_clean: bool | None = None


class PatchPresentation(BaseModel):
    """面向 GitHub 或报告的候选补丁展示格式。"""

    model_config = ConfigDict(extra="forbid")

    inline_suggestion: str | None = None
    full_diff: str | None = None
    warning: str


class PatchProposal(BaseModel):
    """基于确定 Head、尚未执行项目测试的候选补丁。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex)
    issue_ids: list[str] = Field(min_length=1)
    title: str = Field(min_length=1, max_length=300)
    rationale: str = Field(min_length=1, max_length=2_000)
    unified_diff: str = Field(min_length=1)
    touched_files: list[str] = Field(min_length=1)
    risk: Literal["low", "medium", "high"]
    assumptions: list[str] = Field(default_factory=list, max_length=12)
    status: PatchStatus = PatchStatus.unverified
    head_sha: str = Field(min_length=1)
    patch_sha: str | None = None
    revision_of: str | None = None
    attempt_number: int = Field(default=1, ge=1)
    validation_backend: str | None = None
    validation_result_id: str | None = None
    apply_check: PatchApplyCheck = Field(default_factory=PatchApplyCheck)
    presentation: PatchPresentation | None = None
    stale: bool = False

    # 兼容阶段 3 的本地验证结果读取；新候选流程不会写入该字段。
    validation_snapshot_id: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_fields(cls, value: Any) -> Any:
        """只在领域边界迁移旧字段；新模型输出不经过此兼容入口。"""
        if not isinstance(value, dict):
            return value
        data = dict(value)
        legacy_diff = data.pop("diff_content", None)
        legacy_issue = data.pop("issue_id", None)
        if "unified_diff" not in data and legacy_diff is not None:
            data["unified_diff"] = legacy_diff
        if "issue_ids" not in data and legacy_issue:
            data["issue_ids"] = [legacy_issue]
        if legacy_diff is not None:
            data.setdefault("issue_ids", ["legacy-unassigned"])
            data.setdefault("title", "候选修复")
            data.setdefault("rationale", "由阶段 3 兼容数据迁移。")
            data.setdefault("touched_files", ["legacy-unknown"])
            data.setdefault("risk", "low")
            data.setdefault("head_sha", "legacy-head")
        return data

    @field_validator("status", mode="before")
    @classmethod
    def migrate_legacy_status(cls, value: PatchStatus | str) -> PatchStatus | str:
        legacy_statuses = {
            "generated": PatchStatus.suggested,
            "applied": PatchStatus.validation_pending,
            "apply_failed": PatchStatus.abandoned,
            "validation_passed": PatchStatus.verified,
        }
        return legacy_statuses.get(value, value)

    @field_validator("issue_ids")
    @classmethod
    def validate_issue_ids(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not cleaned:
            raise ValueError("issue_ids cannot be empty")
        return cleaned

    @field_validator("touched_files")
    @classmethod
    def validate_touched_files(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(_validate_repo_relative_path(value) for value in values))

    @computed_field(return_type=str | None)
    @property
    def issue_id(self) -> str | None:
        """阶段 3 单 Issue 调用方的只读兼容属性。"""
        return self.issue_ids[0] if self.issue_ids else None

    @computed_field(return_type=str)
    @property
    def diff_content(self) -> str:
        """阶段 3 调用方的只读兼容属性。"""
        return self.unified_diff


class PatchResult(PatchProposal):
    """阶段 3 构造方式的兼容适配器；新代码应使用 PatchProposal。"""

    issue_ids: list[str] = Field(default_factory=lambda: ["legacy-unassigned"])
    title: str = "候选修复"
    rationale: str = "由兼容补丁生成接口创建。"
    unified_diff: str = ""
    touched_files: list[str] = Field(default_factory=lambda: ["legacy-unknown"])
    risk: Literal["low", "medium", "high"] = "low"
    head_sha: str = "legacy-head"

class PatchRelatedSymbol(BaseModel):
    """补丁 prompt 可见的索引符号白名单。"""

    model_config = ConfigDict(extra="forbid")

    file: str
    symbol: str
    type: str
    lines: tuple[int, int]
    signature: str = ""

    @field_validator("file")
    @classmethod
    def validate_file(cls, value: str) -> str:
        return _validate_repo_relative_path(value)


class PatchGenerationRequest(BaseModel):
    """传给 Provider 的最小、不可扩张补丁生成输入。"""

    model_config = ConfigDict(extra="forbid")

    issue: ReviewIssue
    primary_evidence: EvidenceAnchor
    supporting_evidence: list[EvidenceAnchor] = Field(default_factory=list, max_length=12)
    related_symbols: list[PatchRelatedSymbol] = Field(default_factory=list, max_length=12)
    limited_context: list[ContextSnippet] = Field(default_factory=list, max_length=12)
    allowed_files: list[str] = Field(min_length=1)
    max_files: int = Field(ge=1, le=10)
    max_changed_lines: int = Field(ge=1, le=1_000)
    head_sha: str = Field(min_length=1)
    prohibited_operations: list[str] = Field(min_length=1, max_length=20)

    @field_validator("allowed_files")
    @classmethod
    def validate_allowed_files(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(_validate_repo_relative_path(value) for value in values))


class PatchGenerationCandidate(BaseModel):
    """LLM 可返回的补丁字段白名单；ID、状态与 Head 均由服务端写入。"""

    model_config = ConfigDict(extra="forbid")

    issue_ids: list[str] = Field(min_length=1, max_length=8)
    title: str = Field(min_length=1, max_length=300)
    rationale: str = Field(min_length=1, max_length=2_000)
    unified_diff: str = Field(min_length=1)
    touched_files: list[str] = Field(min_length=1, max_length=10)
    risk: Literal["low", "medium", "high"]
    assumptions: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("touched_files")
    @classmethod
    def validate_touched_files(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(_validate_repo_relative_path(value) for value in values))


class PatchGenerationAbandon(BaseModel):
    """无法安全修复时的显式模型输出。"""

    model_config = ConfigDict(extra="forbid")

    issue_ids: list[str] = Field(min_length=1, max_length=8)
    reason: str = Field(min_length=1, max_length=2_000)


class PatchGenerationResponse(BaseModel):
    """Provider 对外部模型响应执行严格 schema 校验。"""

    model_config = ConfigDict(extra="forbid")

    patches: list[PatchGenerationCandidate] = Field(default_factory=list, max_length=20)
    abandons: list[PatchGenerationAbandon] = Field(default_factory=list, max_length=20)


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
    distance: int | None = Field(default=None, ge=0, le=2)
    confidence: float | None = Field(default=None, ge=0, le=1)
    why_retrieved: str | None = None


class IssueVerificationBudget(BaseModel):
    """单次 verifier 调用可见的明确只读预算。"""

    model_config = ConfigDict(extra="forbid")

    remaining_calls: int = Field(ge=0)
    max_output_tokens: int = Field(default=1_200, ge=128, le=4_096)
    max_context_chars: int = Field(default=12_000, ge=0, le=40_000)


class IssueVerificationRequest(BaseModel):
    """独立 verifier 的最小、不可扩张输入。"""

    model_config = ConfigDict(extra="forbid")

    issue: ReviewIssue
    primary_evidence: EvidenceAnchor
    supporting_evidence: list[EvidenceAnchor] = Field(default_factory=list, max_length=12)
    unit_diff: str = Field(max_length=60_000)
    readonly_context: list[ContextSnippet] = Field(default_factory=list, max_length=20)
    applicable_rules: list[str] = Field(default_factory=list, max_length=20)
    budget: IssueVerificationBudget


class ReviewUnitResult(BaseModel):
    review_unit_id: str
    status: ReviewUnitStatus = ReviewUnitStatus.pending
    plan_skipped: bool = False
    plan: UnitReviewPlan | None = None
    plan_status: UnitPlanStatus | None = None
    plan_skip_reason: str | None = None
    plan_error: str | None = None
    issues: list[ReviewIssue] = Field(default_factory=list)
    context_snippets: list[ContextSnippet] = Field(default_factory=list)
    messages: list[AgentEvent] = Field(default_factory=list)
    tool_events: list[ReviewUnitToolEvent] = Field(default_factory=list)
    execution_budget: ExecutionBudget = Field(default_factory=ExecutionBudget)
    model_usages: list[ModelUsage] = Field(default_factory=list)
    error: str | None = None
    human_request: HumanReviewRequest | None = None


class RepoSnapshot(BaseModel):
    """仓库概览快照（RepoIndexer 产出）。"""
    language: str
    languages: list[str] = Field(default_factory=list)
    language_counts: dict[str, int] = Field(default_factory=dict)
    is_mixed_language: bool = False
    framework: str | None = None
    test_framework: str | None = None
    total_files: int


class RepositorySnapshot(RepoSnapshot):
    """传给验证后端的只读仓库能力快照。"""


class ReviewSummary(BaseModel):
    """API 中与 issues/patches/validation 并列的只读审查结果摘要。"""

    mode: ReviewMode = ReviewMode.review
    status: TaskStatus = TaskStatus.queued
    completed: bool = False


class PRPurposeSummary(BaseModel):
    """模型生成的 PR 作用中文概括。"""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=800)

    @field_validator("summary")
    @classmethod
    def require_chinese_summary(cls, value: str) -> str:
        normalized = value.strip()
        if not any("\u4e00" <= char <= "\u9fff" for char in normalized):
            raise ValueError("PR purpose summary must contain Chinese text")
        return normalized


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
    validation_profile: str = Field(default_factory=_default_validation_profile)
    review: ReviewSummary = Field(default_factory=ReviewSummary)
    steps: list[TaskStep] = Field(default_factory=list)
    pr: PullRequestInfo | None = None
    changed_files: list[ChangedFile] = Field(default_factory=list)
    review_units: list[ReviewUnit] = Field(default_factory=list)
    review_unit_results: list[ReviewUnitResult] = Field(default_factory=list)
    model_usages: list[ModelUsage] = Field(default_factory=list)
    model_usage_summary: ModelUsageSummary = Field(default_factory=ModelUsageSummary)
    excluded_files: list[ExcludedReviewFile] = Field(default_factory=list)
    issues: list[ReviewIssue] = Field(default_factory=list)
    issue_metrics: IssueMetrics = Field(default_factory=IssueMetrics)
    issue_metrics: IssueMetrics = Field(default_factory=IssueMetrics)
    context_snippets: list[ContextSnippet] = Field(default_factory=list)
    repo_snapshot: RepoSnapshot | None = None
    project_profile: ProjectProfile | None = None
    static_results: list[TestRunResult] = Field(default_factory=list)
    validation_snapshots: list[ValidationSnapshot] = Field(default_factory=list)
    validation_deltas: list[ValidationDelta] = Field(default_factory=list)
    validation: list[ValidationResult] = Field(default_factory=list)
    patch_eligibility: list[PatchEligibilityDecision] = Field(default_factory=list)
    patches: list[PatchProposal] = Field(default_factory=list)
    test_results: list[TestRunResult] = Field(default_factory=list)
    agent_events: list[AgentEvent] = Field(default_factory=list)
    human_request: HumanReviewRequest | None = None
    report_markdown: str | None = None
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

