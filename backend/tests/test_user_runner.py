import hmac
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from app.models.review import (
    PatchResult,
    PatchValidationRequest,
    ReviewTask,
    TaskStatus,
    ValidationCheck,
    ValidationStatus,
)
from app.graph.nodes.optional_validation import optional_validation_node
from app.models.runner import RunnerRegistrationRequest, RunnerResultSubmission
from app.services.user_runner_service import (
    InvalidRunnerResult,
    RunnerAuthenticationError,
    RunnerAuthorizationError,
    UserRunnerService,
    ValidationRequestConflict,
    ValidationRequestExpired,
)
from app.services.review_service import ReviewService
from app.tools.patch_tool import normalized_patch_sha
from app.validation.user_runner import UserRunnerValidationBackend
from app.validation import NoValidationBackend, ValidationBackendRegistry, ValidationBackendSelector


API_TOKEN = "api-token-" + "a" * 32
HMAC_SECRET = "hmac-secret-" + "b" * 32
PATCH = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a=1\n+a=2\n"


def _service(
    *,
    clock=None,
    claim_timeout: timedelta = timedelta(minutes=10),
    request_timeout: timedelta = timedelta(hours=2),
    redact_values: tuple[str, ...] = (),
) -> UserRunnerService:
    return UserRunnerService(
        {"unit": "project_unit_tests", "lint": "project_lint"},
        clock=clock,
        claim_timeout=claim_timeout,
        request_timeout=request_timeout,
        redact_values=redact_values,
    )


def _register(
    service: UserRunnerService,
    *,
    runner_id: str = "runner-1",
    repositories: list[str] | None = None,
    profiles: list[str] | None = None,
    api_token: str = API_TOKEN,
    hmac_secret: str = HMAC_SECRET,
) -> None:
    service.register(RunnerRegistrationRequest(
        runner_id=runner_id,
        display_name=runner_id,
        allowed_repositories=repositories or ["owner/repo"],
        allowed_profiles=profiles or ["unit"],
        api_token=api_token,
        hmac_secret=hmac_secret,
    ))


def _request(*, profile: str = "unit") -> PatchValidationRequest:
    return PatchValidationRequest(
        task_id="task-1",
        patch_id="patch-1",
        repository_id="owner/repo",
        repository_clone_url="https://github.com/owner/repo.git",
        repository_fetch_ref="feature",
        base_sha="base-sha",
        head_sha="head-sha",
        patch_sha=normalized_patch_sha(PATCH),
        patch_content=PATCH,
        validation_profile=profile,
    )


def _submission(
    request_id: str,
    *,
    now: datetime | None = None,
    runner_id: str = "runner-1",
    head_sha: str = "head-sha",
    patch_sha: str | None = None,
    profile: str = "unit",
    idempotency_key: str = "result-key-0001",
    secret: str = HMAC_SECRET,
    log_summary: str = "1 passed",
) -> RunnerResultSubmission:
    result = RunnerResultSubmission(
        request_id=request_id,
        runner_id=runner_id,
        head_sha=head_sha,
        patch_sha=patch_sha or normalized_patch_sha(PATCH),
        profile=profile,
        checks=[ValidationCheck(name="unit", status=ValidationStatus.passed)],
        exit_status=0,
        duration_ms=1200,
        environment_fingerprint="python=3.12;os=linux",
        submitted_at=now or datetime.now(timezone.utc),
        idempotency_key=idempotency_key,
        signature="0" * 64,
        log_summary=log_summary,
    )
    signature = hmac.new(
        secret.encode(), result.canonical_payload(), hashlib.sha256
    ).hexdigest()
    return result.model_copy(update={"signature": signature})


def test_unregistered_runner_cannot_claim() -> None:
    service = _service()
    summary = service.create_request(_request())

    with pytest.raises(RunnerAuthenticationError):
        service.claim(summary.request_id, "anonymous")


def test_runner_cannot_claim_another_repository() -> None:
    service = _service()
    _register(service, repositories=["other/repo"])
    summary = service.create_request(_request())

    with pytest.raises(RunnerAuthorizationError):
        service.claim(summary.request_id, API_TOKEN)


def test_claim_is_idempotent_and_contains_only_runner_inputs() -> None:
    service = _service()
    _register(service)
    summary = service.create_request(_request())

    first = service.claim(summary.request_id, API_TOKEN)
    second = service.claim(summary.request_id, API_TOKEN)

    assert first == second
    assert set(first.model_dump()) == {
        "request_id", "repository", "base_sha", "head_sha", "patch_content",
        "validation_profile_id", "expires_at",
    }
    serialized = first.model_dump_json()
    assert "OPENAI_API_KEY" not in serialized
    assert API_TOKEN not in serialized
    assert HMAC_SECRET not in serialized


def test_claim_timeout_rejects_late_result() -> None:
    now = [datetime(2026, 8, 1, tzinfo=timezone.utc)]
    service = _service(clock=lambda: now[0], claim_timeout=timedelta(seconds=30))
    _register(service)
    summary = service.create_request(_request())
    service.claim(summary.request_id, API_TOKEN)
    now[0] += timedelta(seconds=31)

    with pytest.raises(ValidationRequestExpired, match="claim"):
        service.submit_result(_submission(summary.request_id, now=now[0]), API_TOKEN)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"head_sha": "wrong-head"}, "head_sha"),
        ({"patch_sha": "0" * 64}, "patch_sha"),
        ({"profile": "missing"}, "profile"),
    ],
)
def test_result_identifiers_must_match(changes: dict[str, str], message: str) -> None:
    service = _service()
    _register(service)
    summary = service.create_request(_request())
    service.claim(summary.request_id, API_TOKEN)

    with pytest.raises(InvalidRunnerResult, match=message):
        service.submit_result(_submission(summary.request_id, **changes), API_TOKEN)


def test_invalid_signature_is_rejected() -> None:
    service = _service()
    _register(service)
    summary = service.create_request(_request())
    service.claim(summary.request_id, API_TOKEN)

    with pytest.raises(InvalidRunnerResult, match="signature"):
        service.submit_result(
            _submission(summary.request_id).model_copy(update={"signature": "f" * 64}),
            API_TOKEN,
        )


def test_expired_request_result_is_rejected() -> None:
    now = [datetime(2026, 8, 1, tzinfo=timezone.utc)]
    service = _service(
        clock=lambda: now[0],
        claim_timeout=timedelta(minutes=5),
        request_timeout=timedelta(seconds=60),
    )
    _register(service)
    summary = service.create_request(_request())
    service.claim(summary.request_id, API_TOKEN)
    now[0] += timedelta(seconds=61)

    with pytest.raises(ValidationRequestExpired):
        service.submit_result(_submission(summary.request_id, now=now[0]), API_TOKEN)


def test_duplicate_result_is_idempotent() -> None:
    service = _service()
    _register(service)
    summary = service.create_request(_request())
    service.claim(summary.request_id, API_TOKEN)
    submission = _submission(summary.request_id)

    first = service.submit_result(submission, API_TOKEN)
    second = service.submit_result(submission, API_TOKEN)

    assert first.receipt.result.id == second.receipt.result.id
    assert second.receipt.idempotent_replay is True


def test_unregistered_profile_is_rejected() -> None:
    service = _service()
    with pytest.raises(RunnerAuthorizationError, match="not registered"):
        service.create_request(_request(profile="unknown"))


def test_cancelled_request_rejects_result() -> None:
    service = _service()
    _register(service)
    summary = service.create_request(_request())
    service.claim(summary.request_id, API_TOKEN)
    service.cancel(summary.request_id)

    with pytest.raises(ValidationRequestConflict, match="cancelled"):
        service.submit_result(_submission(summary.request_id), API_TOKEN)
    assert service.get_summary(summary.request_id).status.value == "cancelled"


def test_runner_log_is_redacted_before_storage() -> None:
    server_key = "server-secret-value"
    service = _service(redact_values=(server_key,))
    _register(service)
    summary = service.create_request(_request())
    service.claim(summary.request_id, API_TOKEN)

    receipt = service.submit_result(
        _submission(summary.request_id, log_summary=f"token={server_key}; hmac={HMAC_SECRET}"),
        API_TOKEN,
    ).receipt

    assert server_key not in (receipt.result.log_summary or "")
    assert HMAC_SECRET not in (receipt.result.log_summary or "")
    assert "[REDACTED]" in (receipt.result.log_summary or "")


@pytest.mark.asyncio
async def test_backend_creates_request_without_waiting_for_online_runner() -> None:
    service = _service()
    backend = UserRunnerValidationBackend(service)

    result = await backend.validate(_request())

    assert result.status == ValidationStatus.unsupported
    assert result.trusted is False
    assert result.validation_request_id
    assert service.get_summary(result.validation_request_id).status.value == "pending"


@pytest.mark.asyncio
async def test_offline_user_runner_keeps_patch_unverified_and_review_can_continue() -> None:
    service = _service()
    backend = UserRunnerValidationBackend(service)
    selector = ValidationBackendSelector(
        ValidationBackendRegistry([NoValidationBackend(), backend])
    )
    patch = PatchResult(
        id="patch-1",
        issue_id="issue-1",
        diff_content=PATCH,
        status="unverified",
        head_sha="head-sha",
        patch_sha=normalized_patch_sha(PATCH),
    )

    result = await optional_validation_node({
        "task_id": "task-1",
        "mode": "review_suggest_and_validate",
        "validation_backend": "user_runner",
        "validation_profile": "unit",
        "base_sha": "base-sha",
        "head_sha": "head-sha",
        "pr_url": "https://github.com/owner/repo/pull/1",
        "pr_info": {
            "owner": "owner",
            "repo": "repo",
            "clone_url": "https://github.com/owner/repo.git",
            "head": {"ref": "feature"},
        },
        "patches": [patch.model_dump(mode="json")],
        "validation_results": [],
        "warnings": [],
        "_validation_backend_selector": selector,
    })

    assert result["patches"][0]["status"] == "unverified"
    assert result["validation_results"][0]["validation_request_id"]
    assert result["validation_results"][0]["trusted"] is False


def test_only_trusted_runner_result_can_verify_review_patch() -> None:
    runner_service = _service()
    _register(runner_service)
    summary = runner_service.create_request(_request())
    runner_service.claim(summary.request_id, API_TOKEN)
    trusted_result = runner_service.submit_result(
        _submission(summary.request_id), API_TOKEN
    ).receipt.result

    review_service = ReviewService(
        github_tool=None,  # type: ignore[arg-type]
        git_tool=None,  # type: ignore[arg-type]
        diff_parser=None,  # type: ignore[arg-type]
        provider=None,  # type: ignore[arg-type]
        report_service=None,  # type: ignore[arg-type]
    )
    patch = PatchResult(
        id="patch-1",
        issue_id="issue-1",
        diff_content=PATCH,
        status="unverified",
        head_sha="head-sha",
        patch_sha=normalized_patch_sha(PATCH),
    )
    task = ReviewTask(
        id="task-1",
        pr_url="https://github.com/owner/repo/pull/1",
        status=TaskStatus.completed,
        mode="review_suggest_and_validate",
        validation_backend="user_runner",
        patches=[patch],
    )
    review_service._tasks[task.id] = task

    with pytest.raises(ValueError, match="does not match"):
        review_service.apply_user_runner_result(
            task.id,
            patch.id,
            trusted_result.model_copy(update={"trusted": False}),
        )
    assert task.patches[0].status.value == "unverified"

    review_service.apply_user_runner_result(task.id, patch.id, trusted_result)
    assert task.patches[0].status.value == "verified"
    assert task.validation[0].trust_source == "user_runner"
    assert task.validation[0].runner_id == "runner-1"
