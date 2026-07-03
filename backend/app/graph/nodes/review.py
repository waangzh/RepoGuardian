import logging
from typing import Any

from app.agents.review_agent import ReviewAgent
from app.core.config import settings
from app.agents.providers import build_provider
from app.graph.nodes._events import append_event, append_step
from app.graph.state import ReviewState
from app.models.review import AgentAction, ChangedFile, ChangedLine, PullRequestInfo

logger = logging.getLogger("RepoGuardian.Node")


async def review_node(state: ReviewState) -> ReviewState:
    """审查节点：调用 LLM 对变更文件进行结构化代码审查。

    将 diff + 上下文片段 + 静态分析结果拼接为"增强 diff"送入 LLM，
    产出结构化的 ReviewIssue 列表（严重性、类别、修复建议等）。
    """
    action = AgentAction.model_validate(state.get("next_action") or {
        "action": "review_code",
        "reason": "执行代码审查",
    })
    changed_files = _rebuild_changed_files(state.get("changed_files") or [])
    if not changed_files:
        message = "无变更文件，跳过 LLM 审查"
        logger.warning("✍️ [审查] 跳过: 无变更文件")
        return ReviewState(
            review_issues=[],
            agent_events=append_event(state, action.action, action.reason, "completed", message),
            step_progress=append_step(state, "review", "completed", message),
        )

    logger.info("✍️ [审查] 开始审查 %d 个变更文件...", len(changed_files))

    provider: Any = state.get("_provider") or build_provider(
        settings.repoguardian_provider,
        settings.openai_api_key,
        settings.openai_base_url,
        settings.repoguardian_model,
    )
    agent = ReviewAgent(provider)
    pr_info = _rebuild_pr_info(state.get("pr_info") or {})
    enhanced_diff = _build_enhanced_diff(state)
    logger.debug("✍️ [审查] 增强 diff 总长度: %d 字符", len(enhanced_diff))

    issues = await agent.review(pr_info, changed_files, enhanced_diff, state.get("model"))
    issues_dicts = [issue.model_dump(mode="json") for issue in issues]
    # 按严重性统计
    severity_counts: dict[str, int] = {}
    for issue in issues:
        severity_counts[issue.severity] = severity_counts.get(issue.severity, 0) + 1
    message = f"发现 {len(issues_dicts)} 个问题"
    logger.info("✍️ [审查] 完成: %d 个问题（严重性分布: %s）", len(issues_dicts), severity_counts)
    return ReviewState(
        review_issues=issues_dicts,
        agent_events=append_event(state, action.action, action.reason, "completed", message),
        step_progress=append_step(state, "review", "completed", message),
    )


def _build_enhanced_diff(state: ReviewState) -> str:
    """拼接增强 diff：上下文片段 + 静态分析结果 + 原始 diff。"""
    sections: list[str] = []
    context_snippets = state.get("context_snippets") or []
    if context_snippets:
        sections.append("## 相关代码上下文")
        for snippet in context_snippets:
            sections.append(
                f"### {snippet.get('relevance', '?')} | "
                f"`{snippet.get('file', '')}`:{snippet.get('start_line', '?')}-{snippet.get('end_line', '?')}"
            )
            sections.append("```python")
            sections.append(snippet.get("content", ""))
            sections.append("```")
    static_results = state.get("static_results") or []
    if static_results:
        sections.append("## 静态分析结果")
        for result in static_results:
            sections.append(
                f"- {result.get('command')} exit={result.get('exit_code')} "
                f"passed={result.get('passed')}"
            )
            stderr = result.get("stderr") or result.get("stdout") or ""
            if stderr:
                sections.append(stderr[:2000])
    sections.append("## Diff")
    sections.append(state.get("diff_text") or "")
    return "\n".join(sections)


def _rebuild_pr_info(data: dict) -> PullRequestInfo:
    from app.models.review import PullRequestRef

    base = data.get("base", {})
    head = data.get("head", {})
    return PullRequestInfo(
        owner=data.get("owner", ""),
        repo=data.get("repo", ""),
        number=data.get("number", 0),
        title=data.get("title", ""),
        html_url=data.get("html_url", ""),
        clone_url=data.get("clone_url", ""),
        base=PullRequestRef(
            ref=base.get("ref", ""),
            sha=base.get("sha", ""),
            repo_clone_url=base.get("repo_clone_url", ""),
        ),
        head=PullRequestRef(
            ref=head.get("ref", ""),
            sha=head.get("sha", ""),
            repo_clone_url=head.get("repo_clone_url", ""),
        ),
    )


def _rebuild_changed_files(data: list[dict]) -> list[ChangedFile]:
    from app.models.review import DiffHunk

    result: list[ChangedFile] = []
    for file_data in data:
        hunks = []
        for hunk_data in file_data.get("hunks", []):
            added = [
                ChangedLine(line_no=line.get("line_no"), content=line.get("content", ""))
                for line in hunk_data.get("added_lines", [])
            ]
            removed = [
                ChangedLine(line_no=line.get("line_no"), content=line.get("content", ""))
                for line in hunk_data.get("removed_lines", [])
            ]
            hunks.append(DiffHunk(
                old_start=hunk_data.get("old_start", 0),
                old_length=hunk_data.get("old_length", 0),
                new_start=hunk_data.get("new_start", 0),
                new_length=hunk_data.get("new_length", 0),
                added_lines=added,
                removed_lines=removed,
            ))
        result.append(ChangedFile(
            file_path=file_data.get("file_path", ""),
            change_type=file_data.get("change_type", "modified"),
            additions=file_data.get("additions", 0),
            deletions=file_data.get("deletions", 0),
            hunks=hunks,
        ))
    return result
