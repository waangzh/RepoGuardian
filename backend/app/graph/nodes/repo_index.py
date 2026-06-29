from datetime import datetime, timezone

from app.graph.state import ReviewState
from app.tools.repo_indexer import RepoIndexer


async def repo_index_node(state: ReviewState) -> ReviewState:
    repo_path = state.get("repo_path", "")
    indexer = RepoIndexer()
    result = await indexer.execute(repo_path=repo_path)

    step_progress: list[dict] = list(state.get("step_progress") or [])
    step_progress.append({
        "node": "repo_index",
        "status": "completed",
        "message": f"已索引 {len(result['file_index'])} 个文件，{len(result['symbol_index'])} 个符号",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return ReviewState(
        file_index=result["file_index"],
        symbol_index=result["symbol_index"],
        project_meta=result["project_meta"],
        step_progress=step_progress,
    )
