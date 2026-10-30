"""Create once, inspect, stop and retry within the original resource boundary."""

import asyncio

from fastapi import APIRouter, Header, HTTPException, Query, Response

from app.api import reviews as _reviews
from app.review.contracts import ReviewRequest
from app.review.domain import Run

router = APIRouter(prefix="/reviews", tags=["reviews"])


@router.post("", response_model=Run, status_code=202)
async def create_review(request: ReviewRequest,
                        idempotency_key: str = Header(min_length=1, max_length=200)):
    try:
        return await _reviews.get_review_service().create(request, idempotency_key)
    except _reviews.GitHubToolError as exc:
        raise HTTPException(exc.api_status, exc.public_detail()) from None
    except ValueError as exc:
        raise HTTPException(409, {"code": str(exc) if str(exc) == "idempotency_key_conflict"
                                 else "review_request_invalid"}) from None


@router.get("")
async def list_reviews(task_status: str | None = Query(None, alias="status"),
        page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
        q: str | None = Query(None, max_length=200), repository: str | None = None,
        pr_number: int | None = Query(None, ge=1), head_sha: str | None = None):
    return await asyncio.to_thread(_reviews.get_review_service().list_tasks, status=task_status,
        page=page, page_size=page_size, q=q, repository=repository, pr_number=pr_number, head_sha=head_sha)


@router.get("/{task_id}", response_model=Run)
async def get_review(task_id: str):
    run = await asyncio.to_thread(_reviews.get_review_service().get_task, task_id)
    if run is None:
        raise HTTPException(404, "Review not found")
    return run


@router.get("/{task_id}/units")
async def get_units(task_id: str):
    await get_review(task_id)
    return await asyncio.to_thread(_reviews.get_review_service().store.work_items, task_id)


@router.delete("/{task_id}", status_code=204)
async def delete_review(task_id: str):
    try:
        deleted = await asyncio.to_thread(_reviews.get_review_service().delete_task, task_id)
    except ValueError as exc:
        raise HTTPException(409, {"code": str(exc)}) from None
    if not deleted:
        raise HTTPException(404, "Review not found")
    return Response(status_code=204)


@router.post("/{task_id}/cancel", status_code=202)
async def cancel_review(task_id: str):
    await get_review(task_id)
    if not await _reviews.get_review_service().cancel_task(task_id):
        raise HTTPException(409, "Review already finished")
    return {"review_id": task_id, "status": "cancelled"}


async def _retry(task_id, overview):
    try:
        return await _reviews.get_review_service().retry(task_id, overview=overview)
    except KeyError:
        raise HTTPException(404, "Review not found") from None
    except ValueError as exc:
        raise HTTPException(409, {"code": str(exc)}) from None


@router.post("/{task_id}/retry", response_model=Run, status_code=202)
async def retry_review(task_id: str):
    return await _retry(task_id, False)


@router.post("/{task_id}/overview/retry", response_model=Run, status_code=202)
async def retry_overview(task_id: str):
    return await _retry(task_id, True)
