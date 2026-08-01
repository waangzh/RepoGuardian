"""User Runner 注册、领取、取消和签名结果上传 API。"""

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.reviews import get_review_service
from app.core.config import settings
from app.models.runner import (
    RunnerRegistration,
    RunnerRegistrationRequest,
    RunnerResultReceipt,
    RunnerResultSubmission,
    ValidationClaim,
    ValidationRequestSummary,
)
from app.services.user_runner_service import (
    InvalidRunnerResult,
    RunnerAuthenticationError,
    RunnerAuthorizationError,
    UserRunnerError,
    ValidationRequestConflict,
    ValidationRequestExpired,
    ValidationRequestNotFound,
)
from app.validation.user_runner import get_user_runner_service


router = APIRouter(tags=["user-runner"])
bearer = HTTPBearer(auto_error=False)


def _bearer_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Runner token required")
    return credentials.credentials


def _require_admin_token(
    token: str | None = Header(default=None, alias="X-RepoGuardian-Admin-Token"),
) -> None:
    expected = settings.repoguardian_runner_registration_token
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Runner administration is not configured",
        )
    if token is None or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin token")


@router.post(
    "/runners/register",
    response_model=RunnerRegistration,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_require_admin_token)],
)
async def register_runner(request: RunnerRegistrationRequest) -> RunnerRegistration:
    """注册 Runner；响应只返回公有元数据，绝不回显长期密钥。"""
    try:
        return get_user_runner_service().register(request)
    except UserRunnerError as exc:
        raise _http_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.post(
    "/validation-requests/{request_id}/claim",
    response_model=ValidationClaim,
)
async def claim_validation_request(
    request_id: str,
    api_token: str = Depends(_bearer_token),
) -> ValidationClaim:
    try:
        return get_user_runner_service().claim(request_id, api_token)
    except UserRunnerError as exc:
        raise _http_error(exc) from exc


@router.post(
    "/validation-requests/{request_id}/result",
    response_model=RunnerResultReceipt,
)
async def submit_validation_result(
    request_id: str,
    submission: RunnerResultSubmission,
    api_token: str = Depends(_bearer_token),
) -> RunnerResultReceipt:
    if submission.request_id != request_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="path request_id does not match result body",
        )
    service = get_user_runner_service()
    try:
        summary = service.get_summary(request_id)
        task = get_review_service().get_task(summary.task_id)
        if task is not None and task.status.value == "cancelled":
            service.cancel(request_id)
        submitted = service.submit_result(submission, api_token)
        get_review_service().apply_user_runner_result(
            submitted.task_id,
            submitted.patch_id,
            submitted.receipt.result,
        )
        return submitted.receipt
    except UserRunnerError as exc:
        raise _http_error(exc) from exc
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review or patch not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post(
    "/validation-requests/{request_id}/cancel",
    response_model=ValidationRequestSummary,
    dependencies=[Depends(_require_admin_token)],
)
async def cancel_validation_request(request_id: str) -> ValidationRequestSummary:
    try:
        return get_user_runner_service().cancel(request_id)
    except UserRunnerError as exc:
        raise _http_error(exc) from exc


@router.get(
    "/validation-requests/{request_id}",
    response_model=ValidationRequestSummary,
    dependencies=[Depends(_require_admin_token)],
)
async def get_validation_request(request_id: str) -> ValidationRequestSummary:
    try:
        return get_user_runner_service().get_summary(request_id)
    except UserRunnerError as exc:
        raise _http_error(exc) from exc


def _http_error(exc: UserRunnerError) -> HTTPException:
    if isinstance(exc, RunnerAuthenticationError):
        code = status.HTTP_401_UNAUTHORIZED
    elif isinstance(exc, RunnerAuthorizationError):
        code = status.HTTP_403_FORBIDDEN
    elif isinstance(exc, ValidationRequestNotFound):
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(exc, ValidationRequestExpired):
        code = status.HTTP_410_GONE
    elif isinstance(exc, ValidationRequestConflict):
        code = status.HTTP_409_CONFLICT
    elif isinstance(exc, InvalidRunnerResult):
        code = status.HTTP_422_UNPROCESSABLE_ENTITY
    else:
        code = status.HTTP_400_BAD_REQUEST
    return HTTPException(status_code=code, detail=str(exc))
