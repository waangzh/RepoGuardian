"""审查 API 路由 —— 任务创建、查询、SSE 进度推送。

端点：
    POST   /api/reviews             创建审查任务（异步启动图执行）
    GET    /api/reviews/{task_id}   获取任务完整状态
    GET    /api/reviews/{task_id}/report  获取 Markdown 报告
    GET    /api/reviews/{task_id}/stream   SSE 实时进度流
"""

import asyncio

from fastapi import APIRouter, HTTPException

from app.api import reviews as _reviews
from app.models.review import (
    TaskStatus,
)
from app.models.review_plan import FindingDisposition, FindingsResponse
from app.services.review.review_findings import finding_views

router = APIRouter(prefix="/reviews", tags=["reviews"])


@router.get("/{task_id}/findings", response_model=FindingsResponse)
async def get_findings(task_id: str) -> FindingsResponse:
    service = _reviews.get_review_service()
    task = await asyncio.to_thread(service.get_task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    saved = await asyncio.to_thread(service.finding_dispositions, task_id)
    return FindingsResponse(items=finding_views(task, saved))


@router.patch("/{task_id}/findings/{finding_id}/disposition", response_model=FindingsResponse)
async def update_finding_disposition(task_id: str, finding_id: str, value: FindingDisposition) -> FindingsResponse:
    service = _reviews.get_review_service()
    task = await asyncio.to_thread(service.get_task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status not in {TaskStatus.completed, TaskStatus.completed_with_warnings, TaskStatus.failed, TaskStatus.cancelled}:
        raise HTTPException(status_code=409, detail="Review must be terminal before human disposition")
    if finding_id not in {item.id for item in finding_views(task, {})}:
        raise HTTPException(status_code=404, detail="Finding not found")
    try:
        await asyncio.to_thread(service.save_finding_disposition, task_id,
                                finding_id, value.model_dump(mode="json"))
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    return await get_findings(task_id)
