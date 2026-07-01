from app.graph.nodes._events import append_event, append_step
from app.graph.state import ReviewState
from app.models.review import AgentAction
from app.tools.static_analyzer import StaticAnalyzerTool


async def static_analysis_node(state: ReviewState) -> ReviewState:
    action = AgentAction.model_validate(state.get("next_action") or {
        "action": "run_static_analysis",
        "reason": "Run static analysis.",
    })
    result = await StaticAnalyzerTool().execute(
        repo_path=state.get("repo_path", ""),
        command=action.tool_args.get("command"),
        timeout_seconds=action.tool_args.get("timeout_seconds", 60),
    )
    previous = list(state.get("static_results") or [])
    current = result["static_results"]
    passed = all(item.get("passed", False) for item in current)
    message = f"Static analysis {'passed' if passed else 'failed'} ({len(current)} run)."
    return ReviewState(
        static_results=previous + current,
        agent_events=append_event(state, action.action, action.reason, "completed", message),
        step_progress=append_step(state, "static_analysis", "completed", message),
    )
