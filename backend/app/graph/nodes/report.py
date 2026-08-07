import logging
from datetime import datetime, timezone

from app.graph.nodes._events import append_step
from app.graph.state import ReviewState
from app.models.review import ReviewPhase
from app.services.review_rebuild import rebuild_task_from_state
from app.services.report_service import ReportService

logger = logging.getLogger("RepoGuardian.Node")


async def report_node(state: ReviewState) -> ReviewState:
    """报告节点：将最终图状态重建为 Pydantic 模型并生成 Markdown 报告。

    这是审查流程的终点，之后图进入 END 状态。
    """
    logger.info("📝 [报告] 开始从状态重建 ReviewTask 并生成报告...")
    final_status = _final_status(state)
    final_phase = ReviewPhase.failed if final_status == "failed" else ReviewPhase.completed
    updated_at = datetime.now(timezone.utc).isoformat()
    report_state = ReviewState(**{
        **state,
        "status": final_status,
        "phase": final_phase,
        "updated_at": updated_at,
    })
    task = rebuild_task_from_state(report_state)
    markdown = ReportService().generate(task)
    logger.info("📝 [报告] 报告生成完成（%d 字符 Markdown）", len(markdown))
    return ReviewState(
        report_markdown=markdown,
        status=final_status,
        phase=final_phase,
        updated_at=updated_at,
        step_progress=append_step(state, "report", "completed", "报告已生成"),
    )


async def complete_node(state: ReviewState) -> ReviewState:
    """报告发布完成后才将任务标记为 completed。"""
    final_status = _final_status(state)
    if final_status == "failed":
        return ReviewState(status=final_status, phase=ReviewPhase.failed)
    return ReviewState(
        status=final_status,
        phase=ReviewPhase.completed,
    )


def _final_status(state: ReviewState) -> str:
    if state.get("status") == "failed":
        return "failed"
    return "completed_with_warnings" if state.get("warnings") else "completed"
