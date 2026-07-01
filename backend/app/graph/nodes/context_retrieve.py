from app.graph.nodes._events import append_event, append_step
from app.graph.state import ReviewState
from app.models.review import AgentAction
from app.tools.code_search import CodeSearchTool


async def context_retrieve_node(state: ReviewState) -> ReviewState:
    action = AgentAction.model_validate(state.get("next_action") or {
        "action": "retrieve_context",
        "reason": "检索相关上下文",
    })
    changed_files = state.get("changed_files") or []
    symbol_index = state.get("symbol_index") or []
    file_index = state.get("file_index") or []
    repo_path = state.get("repo_path", "")

    if not changed_files or not symbol_index:
        message = "无可检索上下文（无变更文件或符号索引为空）"
        return ReviewState(
            context_snippets=[],
            agent_events=append_event(state, action.action, action.reason, "completed", message),
            step_progress=append_step(state, "context_retrieve", "completed", message),
        )

    result = await CodeSearchTool().retrieve_context(changed_files, symbol_index, file_index, repo_path)
    message = f"检索到 {len(result)} 个相关上下文片段"
    return ReviewState(
        context_snippets=result,
        agent_events=append_event(state, action.action, action.reason, "completed", message),
        step_progress=append_step(state, "context_retrieve", "completed", message),
    )
