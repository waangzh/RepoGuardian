"""受控测试节点。修复次数由 ExecutionBudget 管理。"""

import logging

from app.graph.nodes._events import append_event, append_step
from app.graph.state import ReviewState
from app.models.review import AgentAction, CommandId
from app.tools.test_runner import TestRunnerTool

logger = logging.getLogger("RepoGuardian.Node")


async def test_node(state: ReviewState) -> ReviewState:
    """在任务临时 clone 内执行白名单测试命令。"""
    action = AgentAction.model_validate(state.get("next_action") or {
        "action": "run_tests",
        "reason": "Run tests.",
    })
    command_id = action.tool_args.get("command_id", CommandId.python_test_full.value)
    result = await TestRunnerTool().execute(
        repo_path=state.get("repo_path", ""),
        command_id=command_id,
        adapter_id=state.get("project_adapter_id", "python"),
        executor=state.get("_command_executor"),
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
