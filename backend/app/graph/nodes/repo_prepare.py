import asyncio
from datetime import datetime, timezone
from typing import Any

from app.graph.state import ReviewState
from app.tools.github_tool import GitHubTool
from app.tools.git_tool import GitTool


async def repo_prepare_node(state: ReviewState) -> ReviewState:
    github: Any = state.get("_github_tool") or GitHubTool()
    git_tool: Any = state.get("_git_tool") or GitTool()

    pr_url = state["pr_url"]
    pr_info = await github.fetch_pr(pr_url)
    repo_path, diff_text = await asyncio.to_thread(git_tool.clone_and_diff, pr_info)

    step_progress = _append_step(
        state,
        "repo_prepare",
        "completed",
        f"已获取 {pr_info.owner}/{pr_info.repo}#{pr_info.number}",
    )
    return ReviewState(
        pr_info=pr_info.model_dump(),
        repo_path=repo_path,
        diff_text=diff_text,
        step_progress=step_progress,
    )


def _append_step(state: ReviewState, node: str, status: str, message: str) -> list[dict]:
    steps: list[dict] = list(state.get("step_progress") or [])
    steps.append({
        "node": node,
        "status": status,
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return steps
