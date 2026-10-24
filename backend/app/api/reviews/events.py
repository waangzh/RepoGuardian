"""审查 API 路由 —— 任务创建、查询、SSE 进度推送。

端点：
    POST   /api/reviews             创建审查任务（异步启动图执行）
    GET    /api/reviews/{task_id}   获取任务完整状态
    GET    /api/reviews/{task_id}/report  获取 Markdown 报告
    GET    /api/reviews/{task_id}/stream   SSE 实时进度流
"""

import asyncio
import json

from app.api import reviews as _reviews
from fastapi import APIRouter, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse


router = APIRouter(prefix="/reviews", tags=["reviews"])


@router.get("/{task_id}/discussions")
async def get_pr_discussions(task_id: str, page: int = Query(1, ge=1),
                             page_size: int = Query(20, ge=1, le=100),
                             discussion_id: str | None = Query(None, max_length=256),
                             comment_offset: int = Query(0, ge=0),
                             comment_limit: int = Query(20, ge=1, le=100)):
    from app.services.discussions.pr_discussions import discussion_page
    service = _reviews.get_review_service()
    task = await asyncio.to_thread(service.get_task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Review not found")
    try:
        return await discussion_page(task, service.repository, page=page, page_size=page_size,
            discussion_id=discussion_id, comment_offset=comment_offset, comment_limit=comment_limit)
    except LookupError:
        raise HTTPException(status_code=404, detail="Discussion not found") from None
    except (ValueError, OSError):
        raise HTTPException(status_code=409, detail="Discussion snapshot is missing or invalid") from None


@router.get("/{task_id}/stream")
async def stream_review(task_id: str, request: Request) -> EventSourceResponse:
    """SSE 端点：推送步骤状态变化，任务结束时发送 done。"""
    service = _reviews.get_review_service()
    if await asyncio.to_thread(service.get_task, task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")

    async def event_generator():
        last_step_signature: tuple[tuple[object, ...], ...] = ()
        last_patch_signature: tuple[tuple[str, str], ...] = ()
        while True:
            if await request.is_disconnected():
                break
            task = await asyncio.to_thread(service.get_task, task_id)
            if task is None:
                yield {"event": "error", "data": json.dumps({"message": "Task not found"})}
                break

            steps = [s.model_dump(mode="json") for s in task.steps]
            current_signature = tuple(
                (
                    step["name"], step["status"], step.get("message"),
                    json.dumps(step.get("progress"), sort_keys=True),
                    step.get("started_at"), step.get("updated_at"), step.get("finished_at"),
                )
                for step in steps
            )
            if current_signature != last_step_signature:
                for index, step in enumerate(steps):
                    if (
                        index < len(last_step_signature)
                        and current_signature[index] == last_step_signature[index]
                    ):
                        continue
                    yield {
                        "event": "step_progress",
                        "data": json.dumps({
                            "node": step["name"],
                            "status": step["status"],
                            "message": step.get("message", ""),
                            "progress": step.get("progress"),
                            "started_at": step.get("started_at"),
                            "updated_at": step.get("updated_at"),
                        }),
                    }
                last_step_signature = current_signature

            patch_signature = tuple((patch.id, patch.status.value) for patch in task.patches)
            if patch_signature != last_patch_signature:
                for patch in task.patches:
                    yield {
                        "event": "patch_update",
                        "data": json.dumps({
                            "id": patch.id,
                            "issue_ids": patch.issue_ids,
                            "status": patch.status,
                            "touched_files": patch.touched_files,
                            "risk": patch.risk,
                            "apply_check": patch.apply_check.model_dump(mode="json"),
                            "head_sha": patch.head_sha,
                            "stale": patch.stale,
                            "warning": (
                                patch.presentation.warning if patch.presentation else None
                            ),
                        }),
                    }
                last_patch_signature = patch_signature

            if task.status.value in {
                "completed",
                "completed_with_warnings",
                "failed",
                "cancelled",
            }:
                yield {"event": "done", "data": json.dumps({"status": task.status})}
                break

            await asyncio.sleep(1.0)

    return EventSourceResponse(event_generator())
