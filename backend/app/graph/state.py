"""审查状态定义 —— LangGraph StateGraph 中所有节点共享的 TypedDict。

字段按职责分组：
    输入 / PR 元数据 / 仓库 & Diff / 索引 / 上下文 / 静态分析 /
    审查 & 修复 / 测试 & 输出 / 可观测性 / 工具注入
"""

from typing import Any, TypedDict

from app.models.review import ExecutionBudget, ReviewPhase


class ReviewState(TypedDict, total=False):
    # ---- 基础标识 ----
    task_id: str
    mode: str          # 审查模式，当前固定 "pr_review"
    status: str         # pending → running → completed / failed
    phase: ReviewPhase

    # ---- 输入 ----
    pr_url: str | None
    model: str | None   # 可选模型覆盖

    # ---- PR 元数据（PullRequestInfo 序列化）----
    pr_info: dict[str, Any] | None

    # ---- 仓库 ----
    repo_path: str | None    # 克隆到本地的临时路径
    base_sha: str | None
    head_sha: str | None
    project_adapter_id: str | None
    project_profile: dict[str, Any] | None

    # ---- Diff ----
    diff_text: str | None                          # 原始 unified diff
    changed_files: list[dict[str, Any]] | None     # DiffParser 解析后的 ChangedFile 列表

    # ---- 仓库索引 ----
    file_index: list[dict[str, Any]] | None     # [{path, language, size, imports}, ...]
    symbol_index: list[dict[str, Any]] | None   # [{file, symbol, type, lines, signature, calls}, ...]
    project_meta: dict[str, Any] | None         # {language, framework, test_dirs, ...}

    # ---- 上下文 ----
    context_snippets: list[dict[str, Any]] | None  # CodeSearch 返回的相关代码片段

    # ---- 静态分析 ----
    static_results: list[dict[str, Any]] | None

    # ---- 审查 ----
    next_action: dict[str, Any] | None             # 序列化的当前 AgentAction
    review_issues: list[dict[str, Any]] | None     # LLM 审查发现的问题列表

    # ---- 自动修复 ----
    patches: list[dict[str, Any]] | None  # PatchResult 列表
    pending_patch_ids: list[str] | None
    active_patch_id: str | None
    active_patch_validation_passed: bool | None
    execution_budget: dict[str, int] | ExecutionBudget
    repair_enabled: bool

    # ---- 测试结果 ----
    test_results: list[dict[str, Any]] | None  # TestRunResult 列表
    validation_snapshots: list[dict[str, Any]] | None
    validation_deltas: list[dict[str, Any]] | None
    validation_blocked: bool
    validation_ready: bool

    # ---- 输出 ----
    report_markdown: str | None  # 最终 Markdown 报告

    # ---- 可观测性 ----
    error: str | None
    step_progress: list[dict[str, Any]] | None    # 图步骤进度 [{node, status, message, timestamp}]
    agent_events: list[dict[str, Any]] | None     # Agent 事件日志 [{action, reason, status, ...}]

    # ---- 工具注入（用于测试；不进入 checkpoint）----
    _github_tool: Any
    _git_tool: Any
    _diff_parser: Any
    _provider: Any
    _command_executor: Any
    _project_registry: Any
