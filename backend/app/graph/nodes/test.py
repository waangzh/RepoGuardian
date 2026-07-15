"""受控测试节点。修复次数由 ExecutionBudget 管理。"""

import logging

from app.graph.nodes._events import append_event, append_step
from app.graph.state import ReviewState
from app.models.review import AgentAction
from app.tools.test_runner import TestRunnerTool

logger = logging.getLogger("RepoGuardian.Node")


async def test_node(state: ReviewState) -> ReviewState:
    """在任务临时 clone 内执行白名单测试命令。"""
    action = AgentAction.model_validate(state.get("next_action") or {
        "action": "run_tests",
        "reason": "Run tests.",
    })
    command = action.tool_args.get("command")
    timeout = action.tool_args.get("timeout_seconds", 120)
    result = await TestRunnerTool().execute(
        repo_path=state.get("repo_path", ""),
        command=command,
        timeout_seconds=timeout,
    )
    previous = list(state.get("test_results") or [])
    current = result["test_results"]
    passed = all(item.get("passed", False) for item in current)
    message = f"Tests {'passed' if passed else 'failed'} ({len(current)} run)."
    logger.info("[测试] %s", message)
    return ReviewState(
        test_results=previous + current,
        agent_events=append_event(state, action.action, action.reason, "completed", message),
        step_progress=append_step(state, "test_runner", "completed", message),
    )
