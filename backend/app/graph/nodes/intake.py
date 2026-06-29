from datetime import datetime, timezone
from uuid import uuid4

from app.graph.state import ReviewState
from app.tools.github_tool import GitHubTool


async def intake_node(state: ReviewState) -> ReviewState:
    task_id = state.get("task_id") or uuid4().hex
    step_progress: list[dict] = [{
        "node": "intake",
        "status": "completed",
        "message": "PR URL 已接收",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }]
    return ReviewState(
        task_id=task_id,
        mode=state.get("mode", "pr_review"),
        status="running",
        pr_url=state.get("pr_url"),
        model=state.get("model"),
        step_progress=step_progress,
        fix_iteration=0,
        max_fix_iterations=3,
    )
