from app.graph.nodes._events import append_event, append_step
from app.graph.state import ReviewState
from app.models.review import AgentAction
from app.tools.test_runner import TestRunnerTool


async def test_node(state: ReviewState) -> ReviewState:
    action = AgentAction.model_validate(state.get("next_action") or {
        "action": "run_tests",
        "reason": "Run tests.",
    })
    result = await TestRunnerTool().execute(
        repo_path=state.get("repo_path", ""),
        command=action.tool_args.get("command"),
        timeout_seconds=action.tool_args.get("timeout_seconds", 120),
    )
    previous = list(state.get("test_results") or [])
    current = result["test_results"]
    passed = all(item.get("passed", False) for item in current)
    fix_iteration = int(state.get("fix_iteration") or 0)
    if not passed:
        fix_iteration += 1
    message = f"Tests {'passed' if passed else 'failed'} ({len(current)} run)."
    return ReviewState(
        test_results=previous + current,
        fix_iteration=fix_iteration,
        agent_events=append_event(state, action.action, action.reason, "completed", message),
        step_progress=append_step(state, "test_runner", "completed", message),
    )
