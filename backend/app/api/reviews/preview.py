"""Optional fixed-snapshot planning; no model request."""

from fastapi import APIRouter, HTTPException

from app.api import reviews as _reviews
from app.review.contracts import ReviewRequest

router = APIRouter(prefix="/reviews", tags=["reviews"])


@router.post("/preview")
async def preview_review(request: ReviewRequest):
    try:
        return await _reviews.get_review_service().preview(request)
    except _reviews.GitHubToolError as exc:
        raise HTTPException(exc.api_status, exc.public_detail()) from None
    except ValueError:
        raise HTTPException(422, {"code": "review_request_invalid"}) from None
