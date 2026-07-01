from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, HttpUrl


class TaskStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


class StepStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


class Severity(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class IssueCategory(str, Enum):
    correctness = "correctness"
    maintainability = "maintainability"
    performance = "performance"
    security = "security"
    test = "test"


class AgentActionName(str, Enum):
    retrieve_context = "retrieve_context"
    run_static_analysis = "run_static_analysis"
    review_code = "review_code"
    generate_patch = "generate_patch"
    apply_patch = "apply_patch"
    run_tests = "run_tests"
    finish_report = "finish_report"
    request_human = "request_human"


class ReviewCreateRequest(BaseModel):
    pr_url: HttpUrl
    model: str | None = None


class ReviewCreateResponse(BaseModel):
    task_id: str
    status: TaskStatus


class TaskStep(BaseModel):
    name: str
    status: StepStatus = StepStatus.pending
    message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class PullRequestRef(BaseModel):
    ref: str
    sha: str
    repo_clone_url: str


class PullRequestInfo(BaseModel):
    owner: str
    repo: str
    number: int
    title: str
    html_url: str
    clone_url: str
    base: PullRequestRef
    head: PullRequestRef


class ChangedLine(BaseModel):
    line_no: int | None
    content: str


class DiffHunk(BaseModel):
    old_start: int
    old_length: int
    new_start: int
    new_length: int
    added_lines: list[ChangedLine] = Field(default_factory=list)
    removed_lines: list[ChangedLine] = Field(default_factory=list)


class ChangedFile(BaseModel):
    file_path: str
    change_type: str
    additions: int
    deletions: int
    hunks: list[DiffHunk] = Field(default_factory=list)


class ReviewIssue(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    file_path: str
    line_no: int | None = None
    severity: Severity
    category: IssueCategory
    title: str
    description: str
    suggestion: str
    confidence: float = Field(ge=0, le=1)
    auto_fixable: bool = False


class AgentAction(BaseModel):
    action: AgentActionName
    reason: str
    target_issue_ids: list[str] = Field(default_factory=list)
    tool_args: dict[str, Any] = Field(default_factory=dict)


class AgentEvent(BaseModel):
    action: AgentActionName | str
    reason: str
    status: str
    message: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TestRunResult(BaseModel):
    tool: str
    command: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    passed: bool
    duration: float = 0.0


class PatchResult(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    issue_id: str | None = None
    diff_content: str
    status: str = "generated"
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ContextSnippet(BaseModel):
    file: str
    start_line: int
    end_line: int
    content: str
    relevance: str
    symbol: str | None = None


class RepoSnapshot(BaseModel):
    language: str
    framework: str | None = None
    test_framework: str | None = None
    total_files: int


class ReviewTask(BaseModel):
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

