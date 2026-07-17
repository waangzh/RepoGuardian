import logging

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
    task = rebuild_task_from_state(state)
    markdown = ReportService().generate(task)
    logger.info("📝 [报告] 报告生成完成（%d 字符 Markdown）", len(markdown))
    return ReviewState(
        report_markdown=markdown,
        phase=ReviewPhase.publishing,
        step_progress=append_step(state, "report", "completed", "报告已生成"),
    )


async def complete_node(state: ReviewState) -> ReviewState:
    """报告发布完成后才将任务标记为 completed。"""
    return ReviewState(
        status="completed_with_warnings" if state.get("warnings") else "completed",
        phase=ReviewPhase.completed,
    )
