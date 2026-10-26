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
    service = _reviews.get_review_service()
    task = await asyncio.to_thread(service.get_task, task_id)
    if task is None:
        raise HTTPException(404, "Review not found")
    if not task.pr_discussion:
        return {"items": [], "total": 0, "status": "not_included"}
    try:
        from app.models.pr_discussion import DiscussionSnapshot
        raw = await asyncio.to_thread(service._artifacts.read_text, task.pr_discussion["artifact_uri"])
        snapshot = DiscussionSnapshot.model_validate_json(raw)
        if (snapshot.task_id != task_id or snapshot.head_sha != task.snapshot.head_sha
                or snapshot.base_sha != task.snapshot.effective_base_sha):
            raise ValueError("discussion_snapshot_mismatch")
    except (ValueError, OSError):
        raise HTTPException(409, "Discussion snapshot is missing or invalid") from None
    threads = snapshot.threads
    if discussion_id:
        threads = [thread for thread in threads if thread.id == discussion_id]
        if not threads:
            raise HTTPException(404, "Discussion not found")
    start = (page - 1) * page_size
    visible = []
    for thread in threads[start:start + page_size]:
        item = thread.model_dump(mode="json")
        item["comments"] = item["comments"][comment_offset:comment_offset + comment_limit]
        visible.append(item)
    return {"items": visible, "total": len(threads), "page": page, "page_size": page_size,
            "sources": snapshot.sources, "limitations": snapshot.limitations}


@router.get("/{task_id}/stream")
async def stream_review(task_id: str, request: Request) -> EventSourceResponse:
    """SSE 端点：推送步骤状态变化，任务结束时发送 done。"""
    service = _reviews.get_review_service()
    if await asyncio.to_thread(service.get_task, task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")

    async def event_generator():
        last_step_signature: tuple[str, ...] = ()
        while True:
            if await request.is_disconnected():
                break
            task = await asyncio.to_thread(service.get_task, task_id)
            if task is None:
                yield {"event": "error", "data": json.dumps({"message": "Task not found"})}
                break

            steps = [s.model_dump(mode="json") for s in task.steps]
            current_signature = tuple(json.dumps(step, sort_keys=True) for step in steps)
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
                            "progress": step.get("data"),
                            "started_at": step.get("started_at"),
                            "updated_at": step.get("updated_at"),
                        }),
                    }
                last_step_signature = current_signature

            if task.status in {
                "completed",
                "completed_with_warnings",
                "failed",
                "cancelled",
            }:
                yield {"event": "done", "data": json.dumps({"status": task.status})}
                break

            await asyncio.sleep(1.0)

    return EventSourceResponse(event_generator())
