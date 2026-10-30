"""Machine findings and independent human annotations."""

import asyncio

from fastapi import APIRouter, HTTPException

from app.api import reviews as _reviews
from app.api.reviews.tasks import get_review
from app.review.contracts import FindingDisposition

router = APIRouter(prefix="/reviews", tags=["reviews"])


@router.get("/{task_id}/findings")
async def get_findings(task_id: str):
    run = await get_review(task_id)
    candidates = await asyncio.to_thread(_reviews.get_review_service().store.candidates, task_id)
    from app.review.findings import aggregate
    return {"findings": run.report.findings if run.report else aggregate(candidates),
            "candidates": candidates, "report_version": run.report.version if run.report else None,
            "dispositions": await asyncio.to_thread(_reviews.get_review_service().finding_dispositions, task_id)}


@router.patch("/{task_id}/findings/{finding_id}/disposition")
async def update_finding_disposition(task_id: str, finding_id: str, value: FindingDisposition):
    current = await get_findings(task_id)
    if finding_id not in {item.id for item in current["findings"]}:
        raise HTTPException(404, "Finding not found")
    try:
        await asyncio.to_thread(_reviews.get_review_service().save_finding_disposition,
                                task_id, finding_id, value.model_dump())
    except ValueError as exc:
        raise HTTPException(409, {"code": str(exc)}) from None
    return await get_findings(task_id)
