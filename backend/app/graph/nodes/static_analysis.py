import logging

from app.graph.nodes._events import append_event, append_step
from app.graph.state import ReviewState
from app.models.review import AgentAction, CommandId
from app.tools.static_analyzer import StaticAnalyzerTool

logger = logging.getLogger("RepoGuardian.Node")


async def static_analysis_node(state: ReviewState) -> ReviewState:
    """静态分析节点：在克隆仓库中运行白名单静态分析工具（默认 ruff check）。"""
    action = AgentAction.model_validate(state.get("next_action") or {
        "action": "run_static_analysis",
        "reason": "Run static analysis.",
    })
    command_id = action.tool_args.get("command_id", CommandId.python_static_default.value)
    logger.info("🔬 [静态分析] 执行注册命令: %s", command_id)
    result = await StaticAnalyzerTool().execute(
        repo_path=state.get("repo_path", ""),
        command_id=command_id,
        adapter_id=state.get("project_adapter_id", "python"),
        executor=state.get("_command_executor"),
    )
    previous = list(state.get("static_results") or [])
    current = result["static_results"]
    passed = all(item.get("passed", False) for item in current)
    exit_codes = [item.get("exit_code", "?") for item in current]
    message = f"Static analysis {'passed' if passed else 'failed'} ({len(current)} run)."
    logger.info("🔬 [静态分析] %s，exit=%s", "通过" if passed else "失败", exit_codes)
    return ReviewState(
        static_results=previous + current,
        agent_events=append_event(state, action.action, action.reason, "completed", message),
        step_progress=append_step(state, "static_analysis", "completed", message),
    )
