import logging

from app.graph.nodes._events import append_event, append_step
from app.graph.state import ReviewState
from app.models.review import AgentAction
from app.tools.test_runner import TestRunnerTool

logger = logging.getLogger("RepoGuardian.Node")


async def test_node(state: ReviewState) -> ReviewState:
    """测试节点：在克隆仓库中运行白名单测试命令（默认 pytest -q）。

    如果测试失败会递增 fix_iteration，触发 agent_decide 的修复循环限制检查。
    """
    action = AgentAction.model_validate(state.get("next_action") or {
        "action": "run_tests",
        "reason": "Run tests.",
    })
    command = action.tool_args.get("command")
    timeout = action.tool_args.get("timeout_seconds", 120)
    logger.info("🧪 [测试] 执行: %s（超时=%ds）", command or "python -m pytest -q", timeout)
    result = await TestRunnerTool().execute(
        repo_path=state.get("repo_path", ""),
        command=command,
        timeout_seconds=timeout,
    )
    previous = list(state.get("test_results") or [])
    current = result["test_results"]
    passed = all(item.get("passed", False) for item in current)
    fix_iteration = int(state.get("fix_iteration") or 0)
    if not passed:
        fix_iteration += 1
        logger.warning("🧪 [测试] 失败！修复迭代 %d/%d", fix_iteration, state.get("max_fix_iterations", 3))
    else:
        logger.info("🧪 [测试] 通过 ✓ (%d 项)", len(current))
    message = f"Tests {'passed' if passed else 'failed'} ({len(current)} run)."
    return ReviewState(
        test_results=previous + current,
        fix_iteration=fix_iteration,
        agent_events=append_event(state, action.action, action.reason, "completed", message),
        step_progress=append_step(state, "test_runner", "completed", message),
    )
