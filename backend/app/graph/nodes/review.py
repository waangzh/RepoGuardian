from datetime import datetime, timezone

from typing import Any

from app.agents.providers import build_provider
from app.agents.review_agent import ReviewAgent
from app.core.config import settings
from app.graph.state import ReviewState
from app.models.review import ChangedFile, ChangedLine, PullRequestInfo


async def review_node(state: ReviewState) -> ReviewState:
    pr_info_dict = state.get("pr_info") or {}
    pr_info = _rebuild_pr_info(pr_info_dict)
    changed_files = _rebuild_changed_files(state.get("changed_files") or [])
    diff_text = state.get("diff_text") or ""
    model = state.get("model")
    context_snippets = state.get("context_snippets") or []

    provider: Any = state.get("_provider") or build_provider(
        settings.repoguardian_provider,
        settings.openai_api_key,
        settings.openai_base_url,
        settings.repoguardian_model,
    )
    agent = ReviewAgent(provider)

    if changed_files:
        # Inject context into diff for enhanced review
        context_text = _build_context_text(context_snippets)
        enhanced_diff = diff_text
        if context_text:
            enhanced_diff = (
                f"## 相关代码上下文\n{context_text}\n\n## Diff\n{diff_text}"
            )
        issues = await agent.review(pr_info, changed_files, enhanced_diff, model)
        issues_dicts = [i.model_dump() for i in issues]
    else:
        issues_dicts = []

    step_progress: list[dict] = list(state.get("step_progress") or [])
    step_progress.append({
        "node": "review",
        "status": "completed",
        "message": f"发现 {len(issues_dicts)} 个问题",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return ReviewState(
        review_issues=issues_dicts,
        step_progress=step_progress,
    )


def _build_context_text(snippets: list[dict]) -> str:
    if not snippets:
        return ""
    lines = ["## 相关代码上下文"]
    for s in snippets:
        lines.append(
            f"### {s.get('relevance', '?')} | `{s['file']}`:{s.get('start_line', '?')}-{s.get('end_line', '?')}"
        )
        lines.append("```python")
        lines.append(s.get("content", ""))
        lines.append("```")
    return "\n".join(lines)


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
    result: list[ChangedFile] = []
    for f in data:
        hunks = []
        for h in f.get("hunks", []):
            added = [ChangedLine(line_no=a.get("line_no"), content=a.get("content", ""))
                     for a in h.get("added_lines", [])]
            removed = [ChangedLine(line_no=r.get("line_no"), content=r.get("content", ""))
                      for r in h.get("removed_lines", [])]
            from app.models.review import DiffHunk
            hunks.append(DiffHunk(
                old_start=h.get("old_start", 0),
                old_length=h.get("old_length", 0),
                new_start=h.get("new_start", 0),
                new_length=h.get("new_length", 0),
                added_lines=added,
                removed_lines=removed,
            ))
        result.append(ChangedFile(
            file_path=f.get("file_path", ""),
            change_type=f.get("change_type", "modified"),
            additions=f.get("additions", 0),
            deletions=f.get("deletions", 0),
            hunks=hunks,
        ))
    return result
