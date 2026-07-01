from app.graph.nodes._events import append_step
from app.graph.state import ReviewState
from app.tools.repo_indexer import RepoIndexer


async def repo_index_node(state: ReviewState) -> ReviewState:
    result = await RepoIndexer().execute(repo_path=state.get("repo_path", ""))
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
