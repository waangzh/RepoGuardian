"""Current task lifecycle and native graph assignment results."""

import asyncio
from fastapi import APIRouter, HTTPException, Query, Response
from app.api import reviews as _reviews
from app.review.contracts import (
    ReviewCreateRequest, ReviewCreateResponse, ReviewTask, ReviewTaskListResponse, TERMINAL_STATUSES,
)

router = APIRouter(prefix="/reviews", tags=["reviews"])


@router.post("", response_model=ReviewCreateResponse, status_code=202)
async def create_review(request: ReviewCreateRequest):
    try:
        task = await _reviews.get_review_service().create_confirmed_task(request)
    except _reviews.GitHubToolError as exc:
        raise HTTPException(status_code=exc.api_status, detail=exc.public_detail()) from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from None
    return ReviewCreateResponse(task_id=task.id, status=task.status,
                                snapshot=task.snapshot, confirmed_plan=task.configuration.options)


@router.get("", response_model=ReviewTaskListResponse)
async def list_reviews(task_status: str | None = Query(None, alias="status"),
        page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
        q: str | None = Query(None, max_length=200), repository: str | None = Query(None, max_length=512),
        pr_number: int | None = Query(None, ge=1), head_sha: str | None = Query(None, pattern=r"^[a-f0-9]{40,64}$")):
    return await asyncio.to_thread(_reviews.get_review_service().list_tasks, status=task_status,
        page=page, page_size=page_size, q=q, repository=repository, pr_number=pr_number, head_sha=head_sha)


@router.get("/{task_id}", response_model=ReviewTask)
async def get_review(task_id: str):
    task = await asyncio.to_thread(_reviews.get_review_service().get_task, task_id)
    if task is None:
        raise HTTPException(404, "Review not found")
    return task


@router.delete("/{task_id}", status_code=204)
async def delete_review(task_id: str):
    try:
        deleted = await asyncio.to_thread(_reviews.get_review_service().delete_task, task_id)
    except ValueError as exc:
        raise HTTPException(409, {"code": str(exc), "message": "审查仍在执行、投递或等待对账，暂时不能删除。"}) from None
    if not deleted:
        raise HTTPException(404, "Review not found")
    return Response(status_code=204)


@router.post("/{task_id}/cancel", status_code=202)
async def cancel_review(task_id: str):
    task = await get_review(task_id)
    if task.status in TERMINAL_STATUSES:
        raise HTTPException(409, "Review already finished")
    if not await asyncio.to_thread(_reviews.get_review_service().cancel_task, task_id):
        raise HTTPException(409, "Review is not running")
    return {"task_id": task_id, "status": "cancelled"}


@router.post("/{task_id}/resume", response_model=ReviewTask, status_code=202)
async def resume_review(task_id: str):
    try:
        return await _reviews.get_review_service().resume_review(task_id)
    except KeyError:
        raise HTTPException(404, "Review not found") from None
    except ValueError as exc:
        raise HTTPException(409, {"code": str(exc), "message": "当前记录不能继续执行，请重新确认方案。"}) from None


@router.get("/{task_id}/assignments/{assignment_id}")
async def get_review_assignment(task_id: str, assignment_id: str):
    task = await get_review(task_id)
    report = task.review_report
    if report:
        assignment = next((item for item in report.assignments if item.id == assignment_id), None)
        if assignment:
            result = next((item for item in report.discovery_results if item.id == assignment_id), None)
            return {"assignment": assignment, "result": result}
    raise HTTPException(404, "Assignment not found")
