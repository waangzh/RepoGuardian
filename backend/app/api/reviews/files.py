"""审查 API 路由 —— 任务创建、查询、SSE 进度推送。

端点：
    POST   /api/reviews             创建审查任务（异步启动图执行）
    GET    /api/reviews/{task_id}   获取任务完整状态
    GET    /api/reviews/{task_id}/report  获取 Markdown 报告
    GET    /api/reviews/{task_id}/stream   SSE 实时进度流
"""

import asyncio
import httpx

from fastapi import APIRouter, HTTPException, Query

from app.api import reviews as _reviews
from app.core.config import settings
from app.models.file_view import FileDiffResponse, FileContextResponse

router = APIRouter(prefix="/reviews", tags=["reviews"])


@router.get("/{task_id}/files/{file_id}/diff", response_model=FileDiffResponse)
async def get_file_diff(task_id: str, file_id: int, cursor: int = Query(default=0, ge=0),
                        limit: int = Query(default=20, ge=1, le=100)) -> FileDiffResponse:
    task = await asyncio.to_thread(_reviews.get_review_service().get_task, task_id)
    if task is None or file_id < 0 or file_id >= len(task.changed_files):
        raise HTTPException(status_code=404, detail="Task or file not found")
    file = task.changed_files[file_id]
    end = min(len(file.hunks), cursor + limit)
    return FileDiffResponse(file_id=file_id, file_path=file.file_path, old_file_path=file.old_file_path,
        snapshot=task.snapshot, hunks=[hunk.model_dump(mode="json") for hunk in file.hunks[cursor:end]],
        metadata={"change_type": file.change_type, "is_binary": file.is_binary, "hunk_count": len(file.hunks)},
        next_cursor=end if end < len(file.hunks) else None, truncated=end < len(file.hunks))


@router.get("/{task_id}/files/{file_id}/context", response_model=FileContextResponse)
async def get_file_context(task_id: str, file_id: int) -> FileContextResponse:
    task = await asyncio.to_thread(_reviews.get_review_service().get_task, task_id)
    if task is None or file_id < 0 or file_id >= len(task.changed_files):
        raise HTTPException(status_code=404, detail="Task or file not found")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False,
                                     headers=headers) as client:
            return await _reviews.load_file_context(task, file_id, client)
    except _reviews.FileContextError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (httpx.HTTPError, TimeoutError) as exc:
        raise HTTPException(status_code=502, detail="读取固定快照上下文失败，请稍后重试") from exc
