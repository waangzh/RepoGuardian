import logging
import asyncio

from langgraph.runtime import Runtime

from app.graph.nodes._events import append_step
from app.graph.state import ReviewState, ReviewRunContext
from app.models.review import PullRequestInfo
from app.services.preview.preview_input_cache import PreviewInputCache, input_cache_key
from app.tools.repo_indexer import RepoIndexer

logger = logging.getLogger("RepoGuardian.Node")


async def repo_index_node(state: ReviewState, runtime: Runtime[ReviewRunContext] = None) -> ReviewState:
    """索引节点：扫描克隆仓库，构建 Repository Intelligence 图。

    索引：
        file_index   — 文件路径、语言、大小、导入模块
        symbol_index — 多语言声明、签名与调用关系
        repository_graph — 文件、符号与高置信关系边
        project_meta — 语言、框架、测试目录、入口点
    """
    repo_path = state.get("repo_path", "")
    logger.info("📂 [索引] 开始扫描仓库: %s", repo_path)
    options = {}
    version = (state.get("task_runtime_configuration") or {}).get("call_resolution_version")
    if version is not None:
        options["call_resolution_version"] = version
    context = (runtime.context or {}) if runtime else {}
    repository = context.get("coordination_repository") or state.get("_coordination_repository")
    cached = None
    cache = None
    configuration = state.get("task_runtime_configuration") or {}
    if repository and state.get("pr_info") and version:
        pr = PullRequestInfo.model_validate(state["pr_info"])
        # 仅复用已由 repo_prepare 校验的固定快照与实际 Diff；工作树仍独立。
        if state.get("head_sha") == pr.head.sha and state.get("base_sha") == pr.base.sha:
            cache = PreviewInputCache(repository)
            key = input_cache_key(pr, configuration)
            try:
                prepared = await asyncio.to_thread(cache.get, key)
                if (prepared and prepared["diff"] == state.get("diff_text")
                        and (prepared["diff_context"].get("effective_base_sha") or pr.base.sha)
                        == (state.get("effective_base_sha") or pr.base.sha)):
                    cached = prepared["index"]
            except Exception as exc:
                logger.warning("正式审查索引缓存不可用：%s", type(exc).__name__)
    result = cached if cached is not None else await RepoIndexer().execute(repo_path=repo_path, **options)
    if cached is not None:
        logger.info("📂 [索引] 已复用相同快照与 Diff 的静态索引")
    elif cache:
        try:
            await asyncio.to_thread(cache.put, key, {"diff": state["diff_text"], "index": result,
                "diff_context": {"effective_base_sha": state.get("effective_base_sha") or pr.base.sha,
                                 "diff_policy": state.get("diff_policy")}})
        except Exception as exc:
            logger.warning("正式审查索引缓存写入失败：%s", type(exc).__name__)
    logger.info(
        "📂 [索引] 完成: %d 文件, %d 符号, 框架=%s",
        len(result["file_index"]),
        len(result["symbol_index"]),
        result["project_meta"].get("framework", "未知"),
    )
    return ReviewState(
        file_index=result["file_index"],
        symbol_index=result["symbol_index"],
        repository_graph=result["repository_graph"],
        project_meta=result["project_meta"],
        step_progress=append_step(
            state,
            "repo_index",
            "completed",
            f"{'已复用索引' if cached is not None else '已索引'} {len(result['file_index'])} 个文件，{len(result['symbol_index'])} 个符号",
        ),
    )
