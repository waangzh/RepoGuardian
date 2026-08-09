import asyncio
import logging
import threading
from typing import Any

from langgraph.config import get_stream_writer

from app.graph.nodes._events import append_step
from app.graph.state import ReviewState
from app.tools.git_tool import GitProgressEvent, GitTool
from app.tools.github_tool import GitHubTool
from app.core.config import settings

logger = logging.getLogger("RepoGuardian.Node")


async def repo_prepare_node(state: ReviewState) -> ReviewState:
    """准备节点：从 GitHub 拉取 PR 元数据，克隆仓库并生成 unified diff。"""
    github: Any = state.get("_github_tool") or GitHubTool(settings.github_token)
    git_tool: Any = state.get("_git_tool") or GitTool(
        settings.repoguardian_workdir, settings.repoguardian_git_bin
    )
    stream_writer = get_stream_writer()

    def report_progress(event: GitProgressEvent) -> None:
        stream_writer({
            "kind": "git_progress",
            "node": "repo_prepare",
            **event.as_dict(),
        })

    logger.info("📥 [准备] 开始获取 PR 信息: %s", state["pr_url"])
    report_progress(GitProgressEvent(phase="pr_metadata", message="正在获取 PR 元数据"))
    pr_info = await github.fetch_pr(state["pr_url"])
    report_progress(
        GitProgressEvent(
            phase="pr_metadata",
            operation="completed",
            message="PR 元数据获取完成",
            percent=100,
        )
    )
    logger.info("📥 [准备] PR #%d: %s/%s → clone + diff...", pr_info.number, pr_info.owner, pr_info.repo)
    cancel_event = threading.Event()
    clone_task = asyncio.create_task(
        asyncio.to_thread(
            git_tool.clone_and_diff,
            pr_info,
            progress_callback=report_progress,
            cancel_event=cancel_event,
        )
    )
    try:
        repo_path, diff_text = await asyncio.shield(clone_task)
    except asyncio.CancelledError:
        cancel_event.set()
        try:
            await asyncio.wait_for(asyncio.shield(clone_task), timeout=5.0)
        except Exception:
            pass
        raise
    prepared_callback = state.get("_repo_prepared_callback")
    if callable(prepared_callback):
        prepared_callback(repo_path)
    logger.info("📥 [准备] 克隆完成: %s，diff 长度: %d 字符", repo_path, len(diff_text))

    return ReviewState(
        pr_info=pr_info.model_dump(mode="json"),
        repo_path=str(repo_path),
        base_sha=pr_info.base.sha,
        head_sha=pr_info.head.sha,
        diff_text=diff_text,
        step_progress=append_step(
            state,
            "repo_prepare",
            "completed",
            f"已获取 {pr_info.owner}/{pr_info.repo}#{pr_info.number}",
        ),
    )
