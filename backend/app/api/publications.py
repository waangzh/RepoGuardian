"""与只读审查路由隔离的本地单用户手动发布 API。"""
import hmac
from ipaddress import ip_address
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.core.config import settings
from app.models.publication import (PublicationSelection, PublicationAttempt, PublicationAccepted,
    PublicationPreflightRequest, PublicationPreflightResult)
from app.services.publication.publication_repository import PublicationConflict
from app.tools.github.github_publisher import GitHubPublishError


def require_operator(request: Request, authorization: str | None = Header(default=None)):
    if not settings.repoguardian_github_publish_enabled:
        raise HTTPException(503, detail={"code": "publication_disabled"})
    try:
        local = request.client is not None and ip_address(request.client.host).is_loopback
    except ValueError:
        local = False
    origin = request.headers.get("origin")
    if origin:
        try:
            parsed = urlparse(origin)
            parsed.port  # 同时拒绝非法端口；Bearer 不使用自动 Cookie 凭据。
            local = local and parsed.scheme in {"http", "https"} and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        except ValueError:
            local = False
    secret = settings.repoguardian_publication_api_token
    if not local or not secret or not hmac.compare_digest((authorization or "").encode(), ("Bearer " + secret.get_secret_value()).encode()):
        raise HTTPException(403, detail={"code": "publication_operator_unauthorized"})


router = APIRouter(prefix="/reviews", tags=["publications"], dependencies=[Depends(require_operator)])


from app.bootstrap.dependencies import get_publication_service  # noqa: E402


async def domain_call(awaitable):
    try:
        return await awaitable
    except KeyError:
        raise HTTPException(404, detail={"code": "publication_or_review_not_found"}) from None
    except PublicationConflict as exc:
        raise HTTPException(409, detail={"code": str(exc)}) from None
    except GitHubPublishError as exc:
        code = exc.code
        status = (403 if code in {"repository_not_authorized", "github_permission_denied", "publisher_identity_mismatch"}
                  else 503 if code in {"publication_disabled", "github_rate_limited"}
                  else 502 if code.startswith("github_") else 409)
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        raise HTTPException(status, detail={"code": code}, headers=headers) from None


@router.post("/{review_id}/publication-preflight", response_model=PublicationPreflightResult)
async def preflight(review_id: str, body: PublicationPreflightRequest):
    return await domain_call(get_publication_service().preflight(review_id, body.finding_keys))


@router.post("/{review_id}/publications", response_model=PublicationAccepted, status_code=202)
async def create_publication(review_id: str, body: PublicationSelection,
                             idempotency_key: str = Header(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")):
    attempt = await domain_call(get_publication_service().create(review_id, body, idempotency_key))
    return PublicationAccepted(publication_id=attempt.id, state=attempt.state,
        status_url=f"/api/reviews/{review_id}/publications/{attempt.id}")


@router.get("/{review_id}/publications", response_model=list[PublicationAttempt])
async def list_publications(review_id: str):
    return await domain_call(get_publication_service().history(review_id))


@router.get("/{review_id}/publications/{identity}", response_model=PublicationAttempt)
async def get_publication(review_id: str, identity: str):
    return await domain_call(get_publication_service().get(review_id, identity))


@router.post("/{review_id}/publications/{identity}/retry", response_model=PublicationAttempt, status_code=202)
async def retry_publication(review_id: str, identity: str):
    return await domain_call(get_publication_service().retry(review_id, identity))
