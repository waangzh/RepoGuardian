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

from pydantic import BaseModel, Field, HttpUrl


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------

class TaskStatus(str, Enum):
    """审查任务生命周期状态。"""
    pending = "pending"
    running = "running"
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
    """Agent 支持的 8 种操作类型。"""
    retrieve_context = "retrieve_context"
    run_static_analysis = "run_static_analysis"
    review_code = "review_code"
    generate_patch = "generate_patch"
    apply_patch = "apply_patch"
    run_tests = "run_tests"
    finish_report = "finish_report"
    request_human = "request_human"


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


class PatchResult(BaseModel):
    """一个自动修复 patch。"""
    id: str = Field(default_factory=lambda: uuid4().hex)
    issue_id: str | None = None          # 关联的 ReviewIssue ID
    diff_content: str                    # unified diff 内容
    status: str = "generated"            # generated / applied / apply_failed
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
    pr_url: str
    model: str | None = None
    steps: list[TaskStep] = Field(default_factory=list)
    pr: PullRequestInfo | None = None
    changed_files: list[ChangedFile] = Field(default_factory=list)
    issues: list[ReviewIssue] = Field(default_factory=list)
    context_snippets: list[ContextSnippet] = Field(default_factory=list)
    repo_snapshot: RepoSnapshot | None = None
    static_results: list[TestRunResult] = Field(default_factory=list)
    patches: list[PatchResult] = Field(default_factory=list)
    test_results: list[TestRunResult] = Field(default_factory=list)
    agent_events: list[AgentEvent] = Field(default_factory=list)
    report_markdown: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

