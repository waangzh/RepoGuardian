from typing import Any, TypedDict


class ReviewState(TypedDict, total=False):
    task_id: str
    mode: str
    status: str

    # Input
    pr_url: str | None
    model: str | None

    # PR metadata (serialized PullRequestInfo)
    pr_info: dict[str, Any] | None

    # Repository
    repo_path: str | None
    base_sha: str | None
    head_sha: str | None

    # Diff
    diff_text: str | None
    changed_files: list[dict[str, Any]] | None

    # Repo index
    file_index: list[dict[str, Any]] | None
    symbol_index: list[dict[str, Any]] | None
    project_meta: dict[str, Any] | None

    # Context
    context_snippets: list[dict[str, Any]] | None

    # Static analysis (Phase 3)
    static_results: dict[str, Any] | None

    # Review
    next_action: dict[str, Any] | None
    review_issues: list[dict[str, Any]] | None
    fix_decision: list[dict[str, Any]] | None

    # Auto-fix (Phase 4)
    patches: list[dict[str, Any]] | None
    fix_iteration: int
    max_fix_iterations: int

    # Test results (Phase 4)
    test_results: list[dict[str, Any]] | None

    # Output
    report_markdown: str | None

    # Observability
    error: str | None
    step_progress: list[dict[str, Any]] | None
    agent_events: list[dict[str, Any]] | None
    agent_loop_count: int
    max_agent_loops: int
    invalid_action_count: int

    # Tool injection (for testing; not serialized to checkpoint)
    _github_tool: Any
    _git_tool: Any
    _diff_parser: Any
    _provider: Any
