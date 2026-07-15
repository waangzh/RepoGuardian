import logging

from app.graph.nodes._events import append_event, append_step
from app.graph.policies import consume_budget
from app.graph.state import ReviewState
from app.models.review import AgentAction, ReviewPhase
from app.tools.code_search import CodeSearchTool

logger = logging.getLogger("RepoGuardian.Node")


async def context_retrieve_node(state: ReviewState) -> ReviewState:
    """上下文检索节点：为变更符号查找直接代码片段、调用者和测试文件。"""
    action = AgentAction.model_validate(state.get("next_action") or {
        "action": "retrieve_context",
        "reason": "检索相关上下文",
    })
    changed_files = state.get("changed_files") or []
    symbol_index = state.get("symbol_index") or []
    file_index = state.get("file_index") or []
    repo_path = state.get("repo_path", "")
    budget = consume_budget(state, context_retrievals=1)
    if budget is None:
        message = "上下文检索预算已耗尽"
        return ReviewState(
            next_action=None,
            agent_events=append_event(state, action.action, action.reason, "completed", message),
            step_progress=append_step(state, "context_retrieve", "completed", message),
        )

    if not changed_files or not symbol_index:
        message = "无可检索上下文（无变更文件或符号索引为空）"
        logger.warning("🔍 [上下文] 跳过: %s", message)
        return ReviewState(
            next_action=None,
            context_snippets=[],
            phase=ReviewPhase.discovery,
            execution_budget=budget.model_dump(),
            agent_events=append_event(state, action.action, action.reason, "completed", message),
            step_progress=append_step(state, "context_retrieve", "completed", message),
        )

    logger.info("🔍 [上下文] 开始检索 %d 个变更文件的相关上下文...", len(changed_files))
    result = await CodeSearchTool().retrieve_context(changed_files, symbol_index, file_index, repo_path)
    # 统计各类型片段数量
    direct_count = sum(1 for r in result if r.get("relevance") == "direct")
    caller_count = sum(1 for r in result if r.get("relevance") == "caller")
    test_count = sum(1 for r in result if r.get("relevance") == "test")
    message = f"检索到 {len(result)} 个相关上下文片段"
    logger.info(
        "🔍 [上下文] 完成: %d 片段（直接=%d, 调用者=%d, 测试=%d）",
        len(result), direct_count, caller_count, test_count,
    )
    return ReviewState(
        next_action=None,
        context_snippets=result,
        phase=ReviewPhase.discovery,
        execution_budget=budget.model_dump(),
        agent_events=append_event(state, action.action, action.reason, "completed", message),
        step_progress=append_step(state, "context_retrieve", "completed", message),
    )
