from datetime import datetime, timezone

from app.graph.nodes._events import append_step
from app.graph.state import ReviewState
from app.models.review import ReviewTask
from app.services.review_rebuild import rebuild_task_from_state
from app.services.report_service import ReportService


async def report_node(state: ReviewState) -> ReviewState:
    task = rebuild_task_from_state(state)
    markdown = ReportService().generate(task)
    return ReviewState(
        report_markdown=markdown,
        status="completed",
        step_progress=append_step(state, "report", "completed", "报告已生成"),
    )
