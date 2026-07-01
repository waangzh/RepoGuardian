import asyncio
from typing import Any

from app.graph.nodes._events import append_step
from app.graph.state import ReviewState
from app.tools.git_tool import GitTool
from app.tools.github_tool import GitHubTool


async def repo_prepare_node(state: ReviewState) -> ReviewState:
    github: Any = state.get("_github_tool") or GitHubTool()
    git_tool: Any = state.get("_git_tool") or GitTool()

    pr_info = await github.fetch_pr(state["pr_url"])
    repo_path, diff_text = await asyncio.to_thread(git_tool.clone_and_diff, pr_info)

    return ReviewState(
        pr_info=pr_info.model_dump(mode="json"),
        repo_path=str(repo_path),
        diff_text=diff_text,
        step_progress=append_step(
            state,
            "repo_prepare",
            "completed",
            f"已获取 {pr_info.owner}/{pr_info.repo}#{pr_info.number}",
        ),
    )
