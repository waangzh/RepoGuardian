import logging
from uuid import uuid4

from app.graph.nodes._events import append_step
from app.graph.state import ReviewState

logger = logging.getLogger("RepoGuardian.Node")


async def intake_node(state: ReviewState) -> ReviewState:
    """入口节点：初始化任务 ID 和审查流程各计数器，是图的起点。"""
    task_id = state.get("task_id") or uuid4().hex
    logger.info("🏁 [入口] 初始化任务 %s，模式=%s", task_id[:8], state.get("mode", "pr_review"))
    return ReviewState(
        task_id=task_id,
        mode=state.get("mode", "pr_review"),
        status="running",
        pr_url=state.get("pr_url"),
        model=state.get("model"),
        step_progress=append_step(state, "intake", "completed", "已接收 PR URL"),
        fix_iteration=0,
        max_fix_iterations=3,
        agent_loop_count=0,
        max_agent_loops=6,
        invalid_action_count=0,
        agent_events=[],
    )
