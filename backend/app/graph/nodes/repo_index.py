import logging

from app.graph.nodes._events import append_step
from app.graph.state import ReviewState
from app.tools.repo_indexer import RepoIndexer

logger = logging.getLogger("RepoGuardian.Node")


async def repo_index_node(state: ReviewState) -> ReviewState:
    """索引节点：扫描克隆仓库，构建文件级和符号级索引。

    索引：
        file_index   — 文件路径、语言、大小、导入模块
        symbol_index — 函数/类/方法定义、签名、调用关系（tree-sitter 解析）
        project_meta — 语言、框架、测试目录、入口点
    """
    repo_path = state.get("repo_path", "")
    logger.info("📂 [索引] 开始扫描仓库: %s", repo_path)
    result = await RepoIndexer().execute(repo_path=repo_path)
    logger.info(
        "📂 [索引] 完成: %d 文件, %d 符号, 框架=%s",
        len(result["file_index"]),
        len(result["symbol_index"]),
        result["project_meta"].get("framework", "未知"),
    )
    return ReviewState(
        file_index=result["file_index"],
        symbol_index=result["symbol_index"],
        project_meta=result["project_meta"],
        step_progress=append_step(
            state,
            "repo_index",
            "completed",
            f"已索引 {len(result['file_index'])} 个文件，{len(result['symbol_index'])} 个符号",
        ),
    )
