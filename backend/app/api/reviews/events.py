"""Compact SSE snapshots; polling remains sufficient for reconnects."""

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from app.api import reviews as _reviews
from app.review.domain import TERMINAL
from app.review.planner import identity

router = APIRouter(prefix="/reviews", tags=["reviews"])


@router.get("/{task_id}/events")
async def stream_review(task_id: str, request: Request):
    service = _reviews.get_review_service()
    if await asyncio.to_thread(service.get_task, task_id) is None:
        raise HTTPException(404, "Review not found")

    async def events():
        last = None
        while not await request.is_disconnected():
            run = await asyncio.to_thread(service.get_task, task_id)
            if run is None:
                break
            units = await asyncio.to_thread(service.store.work_items, task_id)
            signature = identity([run.updated_at, [item.model_dump() for item in units]])
            if signature != last:
                yield {"event": "task", "data": run.model_dump_json()}
                yield {"event": "progress", "data": json.dumps([item.model_dump() for item in units])}
                last = signature
            if run.status in TERMINAL:
                yield {"event": "done", "data": run.model_dump_json()}
                break
            await asyncio.sleep(1)
    return EventSourceResponse(events())
