"""审查 API 路由 —— 任务创建、查询、SSE 进度推送。

端点：
    POST   /api/reviews             创建审查任务（异步启动图执行）
    GET    /api/reviews/{task_id}   获取任务完整状态
    GET    /api/reviews/{task_id}/report  获取 Markdown 报告
    GET    /api/reviews/{task_id}/stream   SSE 实时进度流
"""

import asyncio
import httpx

from fastapi import APIRouter, HTTPException

from app.api import reviews as _reviews
from app.review.contracts import (
    ReviewPreviewRequest,
    ReviewPreviewResponse,
)
from app.models.quick_preview import QuickPreviewRequest, QuickPreviewResponse
from app.models.preview_job import PreviewJobCreate, PreviewJobResponse

router = APIRouter(prefix="/reviews", tags=["reviews"])


@router.post("/preview-jobs", response_model=PreviewJobResponse, status_code=202)
async def create_preview_job(request: PreviewJobCreate):
    jobs = _reviews.get_preview_jobs()
    try:
        await asyncio.to_thread(jobs.store.create, request.id, request.request)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    jobs.start()
    return await asyncio.to_thread(jobs.store.get, request.id)


@router.get("/preview-jobs/{identity}", response_model=PreviewJobResponse)
async def get_preview_job(identity: str):
    value = await asyncio.to_thread(_reviews.get_preview_jobs().store.get, identity)
    if value is None:
        raise HTTPException(status_code=404, detail="预览不存在，请重新分析 PR。")
    return value


@router.post("/preview-jobs/{identity}/cancel", response_model=PreviewJobResponse)
async def cancel_preview_job(identity: str):
    store = _reviews.get_preview_jobs().store
    await asyncio.to_thread(store.cancel, identity)
    return await get_preview_job(identity)


@router.post("/preview/quick", response_model=QuickPreviewResponse)
async def quick_preview_review(request: QuickPreviewRequest) -> QuickPreviewResponse:
    from app.services.preview.quick_preview import quick_preview, QuickPreviewStale
    from app.tools.github.github_tool import parse_pr_url
    try:
        parse_pr_url(str(request.pr_url))
    except ValueError:
        raise HTTPException(status_code=422, detail="请输入合法的 GitHub PR URL。") from None
    try:
        return await quick_preview(_reviews.get_review_service().github_tool, str(request.pr_url))
    except QuickPreviewStale:
        raise HTTPException(status_code=409, detail="PR快照已变化，请重新获取概览。") from None
    except _reviews.GitHubToolError as exc:
        raise HTTPException(status_code=exc.api_status, detail=exc.public_detail()) from None
    except (TimeoutError, httpx.TimeoutException):
        raise HTTPException(status_code=504, detail="获取 PR 概览超时，请重试或准备完整方案。") from None
    except Exception:
        # GitHub响应可能包含凭据、私有仓库信息或错误正文，不回显上游文本。
        raise HTTPException(status_code=502, detail="获取 PR 概览失败，请检查 GitHub 访问权限后重试。") from None


@router.post("/preview", response_model=ReviewPreviewResponse)
async def preview_review(request: ReviewPreviewRequest) -> ReviewPreviewResponse:
    """返回确定性 Agent 任务和源码范围；不会调用模型或运行目标代码。"""
    try:
        return await _reviews.get_review_service().preview(request)
    except _reviews.GitHubToolError as exc:
        raise HTTPException(status_code=exc.api_status, detail=exc.public_detail()) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
