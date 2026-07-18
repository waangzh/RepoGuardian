"""审查 API 路由 —— 任务创建、查询、SSE 进度推送。

端点：
    POST   /api/reviews             创建审查任务（异步启动图执行）
    GET    /api/reviews/{task_id}   获取任务完整状态
    GET    /api/reviews/{task_id}/report  获取 Markdown 报告
    GET    /api/reviews/{task_id}/stream   SSE 实时进度流
"""

import asyncio
import json
from functools import lru_cache

from fastapi import APIRouter, HTTPException, Request, Response, status
from sse_starlette.sse import EventSourceResponse

from app.agents.providers import build_provider
from app.core.config import settings
from app.models.review import (
    ReviewCreateRequest,
    ReviewCreateResponse,
    ReviewPreviewRequest,
    ReviewPreviewResponse,
    ReviewTask,
    ReviewUnitResult,
)
from app.services.report_service import ReportService
from app.services.review_service import ReviewService
from app.tools.diff_parser import DiffParser
from app.tools.git_tool import GitTool
from app.tools.github_tool import GitHubTool

router = APIRouter(prefix="/reviews", tags=["reviews"])


@lru_cache
def get_review_service() -> ReviewService:
    """获取全局单例 ReviewService（惰性初始化，含所有依赖注入）。"""
    provider = build_provider(
        settings.repoguardian_provider,
        settings.openai_api_key,
        settings.openai_base_url,
        settings.repoguardian_model,
    )
    return ReviewService(
        github_tool=GitHubTool(settings.github_token),
        git_tool=GitTool(settings.repoguardian_workdir, settings.repoguardian_git_bin),
        diff_parser=DiffParser(),
        provider=provider,
        report_service=ReportService(),
    )


@router.post("", response_model=ReviewCreateResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_review(request: ReviewCreateRequest) -> ReviewCreateResponse:
    """创建审查任务，后台启动 LangGraph 执行，立即返回 202。"""
    task = get_review_service().create_task(request)
    return ReviewCreateResponse(task_id=task.id, status=task.status)


@router.post("/preview", response_model=ReviewPreviewResponse)
async def preview_review(request: ReviewPreviewRequest) -> ReviewPreviewResponse:
    """返回确定性 Review Unit 计划；不会调用 LLM、执行器或目标代码。"""
    return await get_review_service().preview(request)


@router.post("/{task_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_review(task_id: str) -> dict[str, str]:
    service = get_review_service()
    if service.get_task(task_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    if not service.cancel_task(task_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Task is not running")
    return {"task_id": task_id, "status": "cancelled"}


@router.post("/{task_id}/units/{unit_id}/retry", response_model=ReviewUnitResult)
async def retry_review_unit(task_id: str, unit_id: str) -> ReviewUnitResult:
    """只重试指定 Review Unit，不重新运行其他成功 Unit。"""
    try:
        return await get_review_service().retry_unit(task_id, unit_id)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task or unit not found")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.get("/{task_id}", response_model=ReviewTask)
async def get_review(task_id: str) -> ReviewTask:
    """按 ID 查询完整任务状态（包含审查问题、patch、测试结果等）。"""
    task = get_review_service().get_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task


@router.get("/{task_id}/report")
async def get_report(task_id: str) -> Response:
    """获取 Markdown 格式的审查报告。"""
    task = get_review_service().get_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    if task.report_markdown is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report is not ready")
    return Response(content=task.report_markdown, media_type="text/markdown; charset=utf-8")


@router.get("/{task_id}/stream")
async def stream_review(task_id: str, request: Request) -> EventSourceResponse:
    """SSE 端点：每秒轮询任务状态，推送步骤进度事件，任务结束时发送 done。"""
    service = get_review_service()

    async def event_generator():
        last_step_count = 0
        while True:
            if await request.is_disconnected():
                break
            task = service.get_task(task_id)
            if task is None:
                yield {"event": "error", "data": json.dumps({"message": "Task not found"})}
                break

            # 推送新完成的步骤
            steps = [s.model_dump() for s in task.steps]
            current_count = len([s for s in steps if s["status"] == "completed"])
            if current_count > last_step_count:
                for step in steps[last_step_count:current_count]:
                    yield {
                        "event": "step_progress",
                        "data": json.dumps({
                            "node": step["name"],
                            "status": "completed",
                            "message": step.get("message", ""),
                        }),
                    }
                last_step_count = current_count

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
