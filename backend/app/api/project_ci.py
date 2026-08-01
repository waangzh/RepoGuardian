"""Project CI workflow_run webhook 与只读状态接口。"""

import hashlib
import hmac
import json
import re

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from app.api.reviews import get_review_service
from app.api.validation_requests import _require_admin_token
from app.core.config import settings
from app.models.project_ci import (
    ProjectCIRequestSummary,
    ProjectCIWebhookReceipt,
    ProjectCIWorkflowRun,
)
from app.services.project_ci_service import (
    ProjectCIEventRejected,
    ProjectCIRequestNotFound,
)
from app.validation.project_ci import get_project_ci_service


router = APIRouter(prefix="/project-ci", tags=["project-ci"])
_REQUEST_TITLE = re.compile(r"^RepoGuardian Validation (?P<request_id>[0-9a-f]{32})$")


def _service():
    service = get_project_ci_service()
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Project CI is not configured",
        )
    return service


@router.post("/webhook", response_model=ProjectCIWebhookReceipt)
async def project_ci_webhook(
    request: Request,
    signature: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    event: str | None = Header(default=None, alias="X-GitHub-Event"),
    delivery_id: str | None = Header(default=None, alias="X-GitHub-Delivery"),
) -> ProjectCIWebhookReceipt:
    body = await request.body()
    secret = settings.repoguardian_github_webhook_secret
    if not secret:
        raise HTTPException(status_code=503, detail="GitHub webhook secret is not configured")
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if signature is None or not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=401, detail="Invalid GitHub webhook signature")
    if event != "workflow_run":
        raise HTTPException(status_code=422, detail="Only workflow_run events are accepted")

    try:
        payload = json.loads(body)
        raw_run = payload["workflow_run"]
        title_match = _REQUEST_TITLE.fullmatch(str(raw_run.get("display_title") or ""))
        if title_match is None:
            raise ProjectCIEventRejected("validation request ID is missing from run title")
        repository = payload["repository"]["full_name"]
        run = ProjectCIWorkflowRun(
            id=raw_run["id"],
            repository=repository,
            workflow_id=raw_run.get("workflow_id"),
            workflow_name=raw_run.get("name") or "",
            ref=raw_run.get("head_branch") or "",
            status=raw_run.get("status") or "",
            conclusion=raw_run.get("conclusion"),
            check_suite_id=raw_run.get("check_suite_id"),
            display_title=raw_run.get("display_title"),
            html_url=raw_run.get("html_url"),
        )
        request_id = title_match.group("request_id")
        service = _service()
        receipt = await service.handle_workflow_run(
            request_id, run, delivery_id=delivery_id
        )
        result = service.get_result(request_id)
        if (
            result is not None
            and service.get_summary(request_id).run_id is not None
            and (result.trust_source or "").startswith("project_ci")
        ):
            summary = service.get_summary(request_id)
            get_review_service().apply_project_ci_result(
                summary.task_id, summary.patch_id, result
            )
        return receipt
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid workflow_run payload") from exc
    except ProjectCIRequestNotFound as exc:
        raise HTTPException(status_code=404, detail="Validation request not found") from exc
    except ProjectCIEventRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/validation-requests/{request_id}", response_model=ProjectCIRequestSummary)
async def get_project_ci_request(
    request_id: str,
    _authorized: None = Depends(_require_admin_token),
) -> ProjectCIRequestSummary:
    try:
        service = _service()
        summary = await service.poll(request_id)
        result = service.get_result(request_id)
        if (
            result is not None
            and summary.run_id is not None
            and (result.trust_source or "").startswith("project_ci")
        ):
            get_review_service().apply_project_ci_result(
                summary.task_id, summary.patch_id, result
            )
        return summary
    except ProjectCIRequestNotFound as exc:
        raise HTTPException(status_code=404, detail="Validation request not found") from exc
    except ProjectCIEventRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/validation-requests/{request_id}/cancel", response_model=ProjectCIRequestSummary)
async def cancel_project_ci_request(
    request_id: str,
    _authorized: None = Depends(_require_admin_token),
) -> ProjectCIRequestSummary:
    try:
        return await _service().cancel(request_id)
    except ProjectCIRequestNotFound as exc:
        raise HTTPException(status_code=404, detail="Validation request not found") from exc
