"""审查 API 路由 —— 任务创建、查询、SSE 进度推送。

端点：
    POST   /api/reviews             创建审查任务（异步启动图执行）
    GET    /api/reviews/{task_id}   获取任务完整状态
    GET    /api/reviews/{task_id}/report  获取 Markdown 报告
    GET    /api/reviews/{task_id}/stream   SSE 实时进度流
"""

import asyncio

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.api import reviews as _reviews
from app.models.review import (
    ReviewCreateRequest,
    ReviewCreateResponse,
    ReviewTask,
    ReviewUnitResult,
    TaskStatus,
)
from app.models.review_runtime import TaskRuntimeRetryResult
from app.models.persistence import (
    ReviewIssueDetail,
    ReviewTaskListResponse,
    ReviewHistoryResult,
    ReviewUnitDetail,
)
from app.services.publication.publication_planner import PublicationPlanner

router = APIRouter(prefix="/reviews", tags=["reviews"])


@router.post("", response_model=ReviewCreateResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_review(request: ReviewCreateRequest) -> ReviewCreateResponse:
    """创建审查任务，后台启动 LangGraph 执行，立即返回 202。"""
    try:
        task = await _reviews.get_review_service().create_confirmed_task(request)
    except _reviews.GitHubToolError as exc:
        raise HTTPException(status_code=exc.api_status, detail=exc.public_detail()) from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc
    return ReviewCreateResponse(task_id=task.id, status=task.status,
        confirmed_plan=task.confirmed_plan, snapshot=task.snapshot, plan_hash=task.plan_hash)


@router.get("", response_model=ReviewTaskListResponse)
async def list_reviews(
    task_status: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    q: str | None = Query(default=None, max_length=200),
    repository: str | None = Query(default=None, max_length=512),
    pr_number: int | None = Query(default=None, ge=1),
    head_sha: str | None = Query(default=None, pattern=r"^[0-9a-f]{40}$"),
) -> ReviewTaskListResponse:
    def load_history():
        result = _reviews.get_review_service().list_tasks(
            status=task_status, page=page, page_size=page_size, q=q,
            repository=repository, pr_number=pr_number, head_sha=head_sha)
        for task in result.items:
            plan = PublicationPlanner().build(task, include_markdown=False)
            task.history_result = ReviewHistoryResult(
                outcome=plan.outcome, merge_decision=plan.digest.merge_decision)
        return result

    return await asyncio.to_thread(load_history)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_review(task_id: str) -> Response:
    try:
        deleted = await asyncio.to_thread(_reviews.get_review_service().delete_task, task_id)
    except ValueError as exc:
        reasons = {
            "only terminal tasks can be deleted": ("review_not_terminal", "审查尚未结束，请先停止审查后再删除。"),
            "review_publication_pending": ("review_publication_pending", "该记录仍在发布或等待确认发布结果，请完成状态核对后再删除。"),
            "review_job_active": ("review_job_active", "该记录仍有后台作业，请等待作业结束后再删除。"),
            "review_delete_persistence_required": ("review_delete_persistence_required", "当前服务未启用持久化，无法删除历史记录。"),
        }
        code, message = reasons.get(str(exc), ("review_delete_conflict", "任务状态已变化，暂时无法删除，请刷新后重试。"))
        raise HTTPException(status_code=503 if code == "review_delete_persistence_required" else 409,
                            detail={"code": code, "message": message}) from None
    if not deleted:
        raise HTTPException(status_code=404, detail="审查记录不存在。")
    return Response(status_code=204)


@router.post("/{task_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_review(task_id: str) -> dict[str, str]:
    service = _reviews.get_review_service()
    task = service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    if task.status in {
        TaskStatus.completed,
        TaskStatus.completed_with_warnings,
        TaskStatus.failed,
        TaskStatus.cancelled,
    }:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Terminal review tasks cannot be cancelled",
        )
    if not service.cancel_task(task_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Task is not running")
    return {"task_id": task_id, "status": "cancelled"}


@router.post("/{task_id}/units/{unit_id}/retry", response_model=ReviewUnitResult | TaskRuntimeRetryResult)
async def retry_review_unit(task_id: str, unit_id: str) -> ReviewUnitResult | TaskRuntimeRetryResult:
    """只重试指定 Review Unit，不重新运行其他成功 Unit。"""
    try:
        return await _reviews.get_review_service().retry_unit(task_id, unit_id)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task or unit not found")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.post("/{task_id}/resume", response_model=TaskRuntimeRetryResult)
async def resume_review(task_id: str, request: ReviewCreateRequest) -> TaskRuntimeRetryResult:
    try:
        return await _reviews.get_review_service().resume_review(task_id, request)
    except KeyError:
        raise HTTPException(status_code=404, detail="Review not found") from None
    except _reviews.GitHubToolError as exc:
        raise HTTPException(status_code=exc.api_status, detail=exc.public_detail()) from None
    except ValueError as exc:
        if str(exc).startswith("review_resume_unavailable:"):
            reason = str(exc).partition(":")[2]
            message = {"deadline": "原审查期限已到，请更新方案并发起全新审查。",
                "budget_exhausted": "原调用、Token 或费用额度已用尽，请发起全新审查。",
                "unknown_attempts": "原审查存在待对账的请求，当前不能继续。",
                "cancelled": "原审查已取消，请发起全新审查。",
                "checkpoint_missing": "原审查恢复记录不完整，当前不能继续。",
                "no_retryable_gap": "原记录没有可继续执行的证据缺口。"}.get(reason, "原审查暂不满足续查条件。")
            raise HTTPException(status_code=409, detail={"code": "review_resume_unavailable",
                "reason": reason, "message": message}) from None
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.get("/{task_id}/units/{unit_id}", response_model=ReviewUnitDetail)
async def get_review_unit(task_id: str, unit_id: str) -> ReviewUnitDetail:
    detail = _reviews.get_review_service().get_unit_detail(task_id, unit_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Review Unit not found")
    return detail


@router.get("/{task_id}/issues/{issue_id}", response_model=ReviewIssueDetail)
async def get_review_issue(task_id: str, issue_id: str) -> ReviewIssueDetail:
    detail = _reviews.get_review_service().get_issue_detail(task_id, issue_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Issue not found")
    return detail


@router.get("/{task_id}", response_model=ReviewTask)
async def get_review(task_id: str) -> ReviewTask:
    """按 ID 查询完整任务状态（包含审查问题、patch、测试结果等）。"""
    task = _reviews.get_review_service().get_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task
