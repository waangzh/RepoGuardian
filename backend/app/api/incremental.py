"""GitHub pull_request webhook：synchronize 后自动创建增量审查。

只做签名校验、allowlist 与幂等投递；审查本身仍走与手工创建相同的
只读预览与冻结流程。
"""

import json

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.api.reviews import get_review_service
from app.core.config import settings
from app.models.incremental import IncrementalWebhookReceipt
from app.services.review.incremental_webhook import (
    IncrementalWebhookService, allowed_repositories, parse_synchronize, verify_signature,
)

router = APIRouter(prefix="/incremental", tags=["incremental"])


@router.post("/webhook", response_model=IncrementalWebhookReceipt)
async def incremental_webhook(
    request: Request,
    signature: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    event: str | None = Header(default=None, alias="X-GitHub-Event"),
    delivery_id: str | None = Header(default=None, alias="X-GitHub-Delivery"),
) -> IncrementalWebhookReceipt:
    body = await request.body()
    if not settings.repoguardian_github_webhook_secret:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="GitHub webhook secret is not configured")
    if not verify_signature(body, signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid GitHub webhook signature")
    if event != "pull_request":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Only pull_request events are accepted")
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Invalid webhook payload") from None
    # 先做无副作用的策略判断，避免未启用时仍构造仓储或触发迁移检查。
    if not settings.repoguardian_incremental_review_webhook_enabled:
        return IncrementalWebhookReceipt(delivery_id=delivery_id or "unknown",
                                         status="skipped", reason="webhook_disabled")
    parsed = parse_synchronize(payload)
    if parsed is None:
        return IncrementalWebhookReceipt(delivery_id=delivery_id or "unknown",
                                         status="skipped", reason="unsupported_event")
    if parsed["repository"].casefold() not in allowed_repositories():
        return IncrementalWebhookReceipt(delivery_id=delivery_id or "unknown",
                                         status="skipped", reason="repository_not_allowed")
    if not delivery_id:
        return IncrementalWebhookReceipt(delivery_id="missing", status="skipped",
                                         reason="delivery_id_missing")
    service = get_review_service()
    repository = service.repository
    if repository is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Persistence is required for incremental review")
    return await IncrementalWebhookService(service, repository).handle(
        payload=payload, delivery_id=delivery_id)
