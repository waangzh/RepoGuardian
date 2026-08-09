"""审查 API 路由 —— 任务创建、查询、SSE 进度推送。

端点：
    POST   /api/reviews             创建审查任务（异步启动图执行）
    GET    /api/reviews/{task_id}   获取任务完整状态
    GET    /api/reviews/{task_id}/report  获取 Markdown 报告
    GET    /api/reviews/{task_id}/stream   SSE 实时进度流
"""

import asyncio
import json
import secrets
from functools import lru_cache

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
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
from app.models.persistence import (
    HumanRequestAnswer,
    HumanRequestAnswerResponse,
    HumanRequestDetail,
    PatchDetail,
    ReviewIssueDetail,
    ReviewTaskListResponse,
    ReviewUnitDetail,
    ValidationDetail,
)
from app.services.report_service import ReportService
from app.services.review_service import ReviewService
from app.services.review_repository import ReviewRepository
from app.services.task_queue import DatabaseTaskQueue
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
        repository=ReviewRepository(),
        task_queue=DatabaseTaskQueue(),
    )


@router.post("", response_model=ReviewCreateResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_review(request: ReviewCreateRequest) -> ReviewCreateResponse:
    """创建审查任务，后台启动 LangGraph 执行，立即返回 202。"""
    task = get_review_service().create_task(request)
    return ReviewCreateResponse(task_id=task.id, status=task.status)


@router.get("", response_model=ReviewTaskListResponse)
async def list_reviews(
    task_status: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> ReviewTaskListResponse:
    return get_review_service().list_tasks(
        status=task_status, page=page, page_size=page_size
    )


@router.post("/preview", response_model=ReviewPreviewResponse)
async def preview_review(request: ReviewPreviewRequest) -> ReviewPreviewResponse:
    """返回确定性 Review Unit 计划；不会调用 LLM、执行器或目标代码。"""
    return await get_review_service().preview(request)


@router.post("/{task_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_review(task_id: str) -> dict[str, str]:
    service = get_review_service()
    if service.get_task(task_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    graph_cancelled = service.cancel_task(task_id)
    from app.validation.project_ci import get_project_ci_service

    project_ci = get_project_ci_service()
    ci_cancelled = await project_ci.cancel_for_task(task_id) if project_ci else False
    if not graph_cancelled and not ci_cancelled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Task is not running")
    from app.validation.user_runner import get_user_runner_service

    get_user_runner_service().cancel_for_task(task_id)
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


@router.get("/{task_id}/units/{unit_id}", response_model=ReviewUnitDetail)
async def get_review_unit(task_id: str, unit_id: str) -> ReviewUnitDetail:
    detail = get_review_service().get_unit_detail(task_id, unit_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Review Unit not found")
    return detail


@router.get("/{task_id}/issues/{issue_id}", response_model=ReviewIssueDetail)
async def get_review_issue(task_id: str, issue_id: str) -> ReviewIssueDetail:
    detail = get_review_service().get_issue_detail(task_id, issue_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Issue not found")
    return detail


@router.get("/{task_id}/patches/{patch_id}", response_model=PatchDetail)
async def get_review_patch(task_id: str, patch_id: str) -> PatchDetail:
    detail = get_review_service().get_patch_detail(task_id, patch_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Patch not found")
    return detail


@router.get("/{task_id}/validations/{validation_id}", response_model=ValidationDetail)
async def get_review_validation(task_id: str, validation_id: str) -> ValidationDetail:
    detail = get_review_service().get_validation_detail(task_id, validation_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Validation not found")
    return detail


@router.get("/{task_id}/human-requests", response_model=list[HumanRequestDetail])
async def list_human_requests(task_id: str) -> list[HumanRequestDetail]:
    if get_review_service().get_task(task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return get_review_service().list_human_request_details(task_id)


@router.get("/{task_id}/human-requests/{request_id}", response_model=HumanRequestDetail)
async def get_human_request(task_id: str, request_id: str) -> HumanRequestDetail:
    detail = get_review_service().get_human_request_detail(task_id, request_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Human request not found")
    return detail


@router.post(
    "/{task_id}/human-requests/{request_id}/answer",
    response_model=HumanRequestAnswerResponse,
)
async def answer_human_request(
    task_id: str,
    request_id: str,
    answer: HumanRequestAnswer,
    authorization: str | None = Header(default=None),
    actor: str | None = Header(default=None, alias="X-RepoGuardian-Actor"),
) -> HumanRequestAnswerResponse:
    expected = settings.repoguardian_human_answer_token
    if not expected:
        raise HTTPException(status_code=503, detail="Human answer authorization is not configured")
    scheme, _, credential = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(credential, expected):
        raise HTTPException(status_code=403, detail="Not authorized to answer this request")
    try:
        detail, replay = get_review_service().answer_human_request(
            task_id,
            request_id,
            answer,
            answered_by=actor or "authorized-user",
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Human request not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return HumanRequestAnswerResponse(request=detail, idempotent_replay=replay)


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
    """SSE 端点：推送步骤状态变化，任务结束时发送 done。"""
    service = get_review_service()

    async def event_generator():
        last_step_signature: tuple[tuple[object, ...], ...] = ()
        last_patch_signature: tuple[tuple[str, str], ...] = ()
        while True:
            if await request.is_disconnected():
                break
            task = service.get_task(task_id)
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
