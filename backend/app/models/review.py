"""领域模型 —— 审查系统的所有 Pydantic 数据模型。

包含：
    - 枚举：TaskStatus, StepStatus, Severity, IssueCategory, AgentActionName
    - API 请求/响应：ReviewCreateRequest, ReviewCreateResponse
    - 领域实体：PullRequestInfo, ChangedFile, ReviewIssue, PatchResult, ...
    - 聚合根：ReviewTask（前端展示的完整状态）
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, HttpUrl, model_validator


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------

class TaskStatus(str, Enum):
    """审查任务生命周期状态。"""
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


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

    generated = "generated"
    apply_failed = "apply_failed"
    applied = "applied"
    validation_passed = "validation_passed"
    validation_failed = "validation_failed"
    abandoned = "abandoned"
    superseded = "superseded"


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

class ReviewCreateRequest(BaseModel):
    """POST /api/reviews 请求体。"""
    pr_url: HttpUrl
    model: str | None = None


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


class AgentAction(BaseModel):
    """LLM 决策节点产出的下一步操作指令。"""
    action: AgentActionName
    reason: str                                    # 选择该操作的中文理由
    target_issue_ids: list[str] = Field(default_factory=list)
    tool_args: dict[str, Any] = Field(default_factory=dict)

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


class PatchResult(BaseModel):
    """一个自动修复 patch。"""
    id: str = Field(default_factory=lambda: uuid4().hex)
    issue_id: str | None = None          # 关联的 ReviewIssue ID
    diff_content: str                    # unified diff 内容
    status: PatchStatus = PatchStatus.generated
    revision_of: str | None = None
    attempt_number: int = Field(default=1, ge=1)
    validation_snapshot_id: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


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


# ---------------------------------------------------------------------------
# 聚合根
# ---------------------------------------------------------------------------

class ReviewTask(BaseModel):
    """审查任务聚合根，聚合所有阶段的产出，供前端完整展示。"""
    id: str
    status: TaskStatus = TaskStatus.pending
    phase: ReviewPhase = ReviewPhase.prepare
    pr_url: str
    model: str | None = None
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
    patches: list[PatchResult] = Field(default_factory=list)
    test_results: list[TestRunResult] = Field(default_factory=list)
    agent_events: list[AgentEvent] = Field(default_factory=list)
    report_markdown: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

