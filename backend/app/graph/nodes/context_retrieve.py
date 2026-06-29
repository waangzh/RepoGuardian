from datetime import datetime, timezone

from app.graph.state import ReviewState
from app.tools.code_search import CodeSearchTool


async def context_retrieve_node(state: ReviewState) -> ReviewState:
    changed_files = state.get("changed_files") or []
    symbol_index = state.get("symbol_index") or []
    file_index = state.get("file_index") or []
    repo_path = state.get("repo_path", "")

    if not changed_files or not symbol_index:
        step_progress: list[dict] = list(state.get("step_progress") or [])
        step_progress.append({
            "node": "context_retrieve",
            "status": "completed",
            "message": "无可检索的上下文（无变更文件或符号索引为空）",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        return ReviewState(context_snippets=[], step_progress=step_progress)

    tool = CodeSearchTool()
    result = await tool.retrieve_context(changed_files, symbol_index, file_index, repo_path)

    step_progress: list[dict] = list(state.get("step_progress") or [])
    step_progress.append({
        "node": "context_retrieve",
        "status": "completed",
        "message": f"检索到 {len(result)} 个相关上下文片段",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return ReviewState(context_snippets=result, step_progress=step_progress)
