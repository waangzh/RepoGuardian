import asyncio
import logging
import threading
from typing import Any

from langgraph.config import get_stream_writer
from langgraph.runtime import Runtime

from app.graph.nodes._events import append_step
from app.graph.state import ReviewState, ReviewRunContext
from app.services.prepared_workspaces import prepare_repository
from app.tools.git_tool import GitProgressEvent, GitTool
from app.tools.github_tool import GitHubTool
from app.core.config import settings

logger = logging.getLogger("RepoGuardian.Node")


async def repo_prepare_node(state: ReviewState, runtime: Runtime[ReviewRunContext] = None) -> ReviewState:
    """准备节点：从 GitHub 拉取 PR 元数据，克隆仓库并生成 unified diff。"""
    github: Any = state.get("_github_tool") or GitHubTool(settings.github_token)
    context = (runtime.context or {}) if runtime else {}
    repository = context.get("coordination_repository") or state.get("_coordination_repository")
    git_tool: Any = context.get("task_runtime_git") or state.get("_git_tool") or GitTool(
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
    frozen = (state.get("task_runtime_configuration") or {}).get("snapshot")
    if frozen and (pr_info.head.sha != frozen["head_sha"] or pr_info.base.sha != frozen["declared_base_sha"]):
        raise ValueError("review_plan_stale: PR changed while queued")
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
            prepare_repository,
            pr_info,
            git_tool,
            repository,
            state.get("task_runtime_configuration") or {},
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
    context_reader = getattr(git_tool, "review_diff_context", None)
    # 内容基准由 Git 工具按本次 PR 的 base/head 重新计算，不使用共享实例的缓存值。
    diff_context = (
        await asyncio.to_thread(context_reader, repo_path, pr_info.base.sha, pr_info.head.sha)
        if callable(context_reader) else {}
    )
    if frozen and (diff_context.get("effective_base_sha") or pr_info.base.sha) != frozen["effective_base_sha"]:
        raise ValueError("review_plan_stale: effective base changed")
    if diff_context.get("diff_policy") == "merge_base":
        expected = state.get("effective_base_sha")
        if expected and expected != diff_context.get("effective_base_sha"):
            raise ValueError("prepared effective base differs from graph input")
    if callable(prepared_callback):
        prepared_callback(repo_path)
    logger.info("📥 [准备] 克隆完成: %s，diff 长度: %d 字符", repo_path, len(diff_text))

    return ReviewState(
        pr_info=pr_info.model_dump(mode="json"),
        repo_path=str(repo_path),
        base_sha=pr_info.base.sha,
        head_sha=pr_info.head.sha,
        **diff_context,
        diff_text=diff_text,
        step_progress=append_step(
            state,
            "repo_prepare",
            "completed",
            f"已获取 {pr_info.owner}/{pr_info.repo}#{pr_info.number}",
        ),
    )
