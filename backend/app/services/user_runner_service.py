"""User Runner 的注册、claim lease、验签与结果幂等服务。"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Callable
from uuid import uuid4

from app.models.review import (
    PatchValidationRequest,
    PatchValidationResult,
    ValidationStatus,
)
from app.models.runner import (
    RepositoryCheckoutInfo,
    RunnerRegistration,
    RunnerRegistrationRequest,
    RunnerResultReceipt,
    RunnerResultSubmission,
    ValidationClaim,
    ValidationRequestStatus,
    ValidationRequestSummary,
)
from app.tools.patch_tool import normalized_patch_sha


class UserRunnerError(RuntimeError):
    """User Runner 协议错误基类。"""


class RunnerAuthenticationError(UserRunnerError):
    pass


class RunnerAuthorizationError(UserRunnerError):
    pass


class ValidationRequestNotFound(UserRunnerError):
    pass


class ValidationRequestConflict(UserRunnerError):
    pass


class ValidationRequestExpired(UserRunnerError):
    pass


class InvalidRunnerResult(UserRunnerError):
    pass


@dataclass(slots=True)
class _RegisteredRunner:
    public: RunnerRegistration
    api_token_hash: bytes
    hmac_secret: bytes


@dataclass(slots=True)
class _ValidationRequest:
    request_id: str
    task_id: str
    patch_id: str
    repository_id: str
    clone_url: str
    fetch_ref: str | None
    base_sha: str
    head_sha: str
    patch_sha: str
    patch_content: str
    profile: str
    created_at: datetime
    expires_at: datetime
    status: ValidationRequestStatus = ValidationRequestStatus.pending
    runner_id: str | None = None
    claimed_at: datetime | None = None
    claim_expires_at: datetime | None = None
    result: PatchValidationResult | None = None

    def summary(self) -> ValidationRequestSummary:
        return ValidationRequestSummary(
            request_id=self.request_id,
            task_id=self.task_id,
            patch_id=self.patch_id,
            repository_id=self.repository_id,
            profile=self.profile,
            status=self.status,
            runner_id=self.runner_id,
            expires_at=self.expires_at,
            claim_expires_at=self.claim_expires_at,
        )


@dataclass(slots=True)
class SubmittedRunnerResult:
    receipt: RunnerResultReceipt
    task_id: str
    patch_id: str


class UserRunnerService:
    """进程内 Runner 协议状态；与当前 ReviewTask 的内存生命周期保持一致。"""

    def __init__(
        self,
        profiles: dict[str, str],
        *,
        claim_timeout: timedelta = timedelta(minutes=10),
        request_timeout: timedelta = timedelta(hours=2),
        clock: Callable[[], datetime] | None = None,
        redact_values: tuple[str, ...] = (),
        max_log_summary_chars: int = 8_000,
    ) -> None:
        if not profiles:
            raise ValueError("at least one validation profile must be registered")
        self._profiles = dict(profiles)
        self._claim_timeout = claim_timeout
        self._request_timeout = request_timeout
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._redact_values = tuple(value for value in redact_values if value)
        self._max_log_summary_chars = max_log_summary_chars
        self._runners: dict[str, _RegisteredRunner] = {}
        self._requests: dict[str, _ValidationRequest] = {}
        self._request_keys: dict[tuple[str, str, str], str] = {}
        self._result_idempotency: dict[
            tuple[str, str], tuple[bytes, RunnerResultReceipt]
        ] = {}
        self._lock = RLock()

    @property
    def profile_ids(self) -> tuple[str, ...]:
        return tuple(self._profiles)

    def register(self, request: RunnerRegistrationRequest) -> RunnerRegistration:
        api_token = request.api_token.get_secret_value()
        hmac_secret = request.hmac_secret.get_secret_value()
        if len(api_token) < 32 or len(hmac_secret) < 32:
            raise ValueError("runner token and HMAC secret must each contain at least 32 characters")
        unknown_profiles = set(request.allowed_profiles) - self._profiles.keys()
        if unknown_profiles:
            raise RunnerAuthorizationError(
                f"runner references unregistered profiles: {sorted(unknown_profiles)}"
            )
        fingerprint = hashlib.sha256(hmac_secret.encode("utf-8")).hexdigest()
        public = RunnerRegistration(
            runner_id=request.runner_id,
            display_name=request.display_name,
            public_key=f"hmac-sha256:{fingerprint}",
            allowed_repositories=request.allowed_repositories,
            allowed_profiles=request.allowed_profiles,
            enabled=request.enabled,
        )
        registered = _RegisteredRunner(
            public=public,
            api_token_hash=hashlib.sha256(api_token.encode("utf-8")).digest(),
            hmac_secret=hmac_secret.encode("utf-8"),
        )
        with self._lock:
            existing = self._runners.get(request.runner_id)
            if existing is not None:
                same_credentials = (
                    hmac.compare_digest(existing.api_token_hash, registered.api_token_hash)
                    and hmac.compare_digest(existing.hmac_secret, registered.hmac_secret)
                )
                if not same_credentials or existing.public != public:
                    raise ValidationRequestConflict("runner_id is already registered")
                return existing.public
            self._runners[request.runner_id] = registered
        return public

    def create_request(self, request: PatchValidationRequest) -> ValidationRequestSummary:
        if not request.validation_profile or request.validation_profile not in self._profiles:
            raise RunnerAuthorizationError("validation profile is not registered")
        if not request.repository_clone_url or request.patch_content is None:
            raise InvalidRunnerResult("runner request is missing repository or patch input")
        if normalized_patch_sha(request.patch_content) != request.patch_sha:
            raise InvalidRunnerResult("patch content does not match patch_sha")

        now = self._now()
        key = (request.task_id, request.patch_id, request.validation_profile)
        with self._lock:
            existing_id = self._request_keys.get(key)
            if existing_id is not None:
                existing = self._requests[existing_id]
                same_input = (
                    existing.repository_id == request.repository_id
                    and existing.head_sha == request.head_sha
                    and existing.patch_sha == request.patch_sha
                )
                if not same_input:
                    raise ValidationRequestConflict(
                        "validation request key was reused for different input"
                    )
                return existing.summary()
            record = _ValidationRequest(
                request_id=uuid4().hex,
                task_id=request.task_id,
                patch_id=request.patch_id,
                repository_id=request.repository_id,
                clone_url=request.repository_clone_url,
                fetch_ref=request.repository_fetch_ref,
                base_sha=request.base_sha,
                head_sha=request.head_sha,
                patch_sha=request.patch_sha,
                patch_content=request.patch_content,
                profile=request.validation_profile,
                created_at=now,
                expires_at=now + self._request_timeout,
            )
            self._requests[record.request_id] = record
            self._request_keys[key] = record.request_id
            return record.summary()

    def claim(self, request_id: str, api_token: str) -> ValidationClaim:
        runner = self.authenticate(api_token)
        now = self._now()
        with self._lock:
            record = self._get_request(request_id)
            self._expire_request(record, now)
            if record.status == ValidationRequestStatus.expired:
                raise ValidationRequestExpired("validation request has expired")
            if record.status in {
                ValidationRequestStatus.cancelled,
                ValidationRequestStatus.completed,
            }:
                raise ValidationRequestConflict(
                    f"validation request is {record.status.value}"
                )
            self._authorize(runner.public, record)

            active_claim = (
                record.runner_id is not None
                and record.claim_expires_at is not None
                and record.claim_expires_at > now
            )
            if active_claim and record.runner_id != runner.public.runner_id:
                raise ValidationRequestConflict("validation request already has an active claim")
            if not active_claim:
                record.runner_id = runner.public.runner_id
                record.claimed_at = now
                record.claim_expires_at = min(
                    now + self._claim_timeout,
                    record.expires_at,
                )
            record.status = ValidationRequestStatus.claimed
            return self._claim_payload(record)

    def submit_result(
        self,
        submission: RunnerResultSubmission,
        api_token: str,
    ) -> SubmittedRunnerResult:
        runner = self.authenticate(api_token)
        if runner.public.runner_id != submission.runner_id:
            raise RunnerAuthenticationError("runner_id does not match bearer token")
        expected_signature = hmac.new(
            runner.hmac_secret,
            submission.canonical_payload(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected_signature, submission.signature.lower()):
            raise InvalidRunnerResult("runner result signature is invalid")

        fingerprint = hashlib.sha256(submission.canonical_payload()).digest()
        idempotency_key = (submission.runner_id, submission.idempotency_key)
        now = self._now()
        with self._lock:
            record = self._get_request(submission.request_id)
            replay = self._result_idempotency.get(idempotency_key)
            if replay is not None:
                previous_fingerprint, previous_receipt = replay
                if not hmac.compare_digest(previous_fingerprint, fingerprint):
                    raise ValidationRequestConflict(
                        "idempotency key was reused for a different result"
                    )
                return SubmittedRunnerResult(
                    receipt=previous_receipt.model_copy(update={"idempotent_replay": True}),
                    task_id=record.task_id,
                    patch_id=record.patch_id,
                )

            if (
                record.status == ValidationRequestStatus.claimed
                and record.claim_expires_at is not None
                and record.claim_expires_at <= now
            ):
                raise ValidationRequestExpired("validation claim has expired")
            self._expire_request(record, now)
            if record.status == ValidationRequestStatus.cancelled:
                raise ValidationRequestConflict("validation request is cancelled")
            if record.status == ValidationRequestStatus.expired:
                raise ValidationRequestExpired("validation request has expired")
            if record.status == ValidationRequestStatus.completed:
                raise ValidationRequestConflict("validation request already has a result")
            if (
                record.status != ValidationRequestStatus.claimed
                or record.runner_id != submission.runner_id
            ):
                raise RunnerAuthorizationError("runner does not hold this claim")
            if record.claim_expires_at is None or record.claim_expires_at <= now:
                raise ValidationRequestExpired("validation claim has expired")
            self._validate_result_identifiers(record, submission, now)

            status = self._result_status(submission)
            result = PatchValidationResult(
                backend="user_runner",
                status=status,
                head_sha=record.head_sha,
                patch_sha=record.patch_sha,
                checks=submission.checks,
                resolved_failures=[],
                new_failures=[],
                environment_fingerprint=submission.environment_fingerprint,
                trusted=True,
                trust_source="user_runner",
                runner_id=submission.runner_id,
                profile=submission.profile,
                exit_status=submission.exit_status,
                duration_ms=submission.duration_ms,
                log_summary=self._redact(submission.log_summary),
                artifact_references=submission.artifact_references,
                started_at=submission.submitted_at
                - timedelta(milliseconds=submission.duration_ms),
                completed_at=submission.submitted_at,
            )
            record.result = result
            record.status = ValidationRequestStatus.completed
            receipt = RunnerResultReceipt(request_id=record.request_id, result=result)
            self._result_idempotency[idempotency_key] = (fingerprint, receipt)
            return SubmittedRunnerResult(
                receipt=receipt,
                task_id=record.task_id,
                patch_id=record.patch_id,
            )

    def cancel(self, request_id: str) -> ValidationRequestSummary:
        with self._lock:
            record = self._get_request(request_id)
            if record.status == ValidationRequestStatus.completed:
                raise ValidationRequestConflict("completed validation cannot be cancelled")
            record.status = ValidationRequestStatus.cancelled
            record.claimed_at = None
            record.claim_expires_at = None
            return record.summary()

    def cancel_for_task(self, task_id: str) -> None:
        with self._lock:
            for record in self._requests.values():
                if record.task_id == task_id and record.status not in {
                    ValidationRequestStatus.completed,
                    ValidationRequestStatus.cancelled,
                }:
                    record.status = ValidationRequestStatus.cancelled
                    record.claimed_at = None
                    record.claim_expires_at = None

    def get_summary(self, request_id: str) -> ValidationRequestSummary:
        with self._lock:
            record = self._get_request(request_id)
            self._expire_request(record, self._now())
            return record.summary()

    def authenticate(self, api_token: str) -> _RegisteredRunner:
        candidate = hashlib.sha256(api_token.encode("utf-8")).digest()
        with self._lock:
            for runner in self._runners.values():
                if hmac.compare_digest(runner.api_token_hash, candidate):
                    if not runner.public.enabled:
                        raise RunnerAuthenticationError("runner is disabled")
                    return runner
        raise RunnerAuthenticationError("invalid runner token")

    def _authorize(self, runner: RunnerRegistration, record: _ValidationRequest) -> None:
        repositories = {value.casefold() for value in runner.allowed_repositories}
        if "*" not in repositories and record.repository_id.casefold() not in repositories:
            raise RunnerAuthorizationError("runner is not authorized for this repository")
        if record.profile not in runner.allowed_profiles:
            raise RunnerAuthorizationError("runner is not authorized for this profile")

    def _validate_result_identifiers(
        self,
        record: _ValidationRequest,
        submission: RunnerResultSubmission,
        now: datetime,
    ) -> None:
        if submission.request_id != record.request_id:
            raise InvalidRunnerResult("request_id mismatch")
        if submission.head_sha != record.head_sha:
            raise InvalidRunnerResult("head_sha mismatch")
        if submission.patch_sha != record.patch_sha:
            raise InvalidRunnerResult("patch_sha mismatch")
        if submission.profile != record.profile or submission.profile not in self._profiles:
            raise InvalidRunnerResult("validation profile mismatch")
        submitted_at = submission.submitted_at
        if submitted_at.tzinfo is None:
            raise InvalidRunnerResult("submitted_at must include a timezone")
        if submitted_at > now + timedelta(minutes=5):
            raise InvalidRunnerResult("submitted_at is too far in the future")
        if record.claimed_at is not None and submitted_at < record.claimed_at:
            raise InvalidRunnerResult("result was produced before the active claim")
        if submitted_at > record.expires_at:
            raise ValidationRequestExpired("runner result was produced after request expiry")

    def _result_status(self, submission: RunnerResultSubmission) -> ValidationStatus:
        if (
            submission.exit_status == 0
            and submission.checks
            and all(check.status == ValidationStatus.passed for check in submission.checks)
        ):
            return ValidationStatus.passed
        for status in (
            ValidationStatus.infrastructure_error,
            ValidationStatus.timed_out,
            ValidationStatus.cancelled,
            ValidationStatus.inconclusive,
            ValidationStatus.unsupported,
        ):
            if any(check.status == status for check in submission.checks):
                return status
        return ValidationStatus.failed

    def _claim_payload(self, record: _ValidationRequest) -> ValidationClaim:
        assert record.claim_expires_at is not None
        return ValidationClaim(
            request_id=record.request_id,
            repository=RepositoryCheckoutInfo(
                repository_id=record.repository_id,
                clone_url=record.clone_url,
                fetch_ref=record.fetch_ref,
            ),
            base_sha=record.base_sha,
            head_sha=record.head_sha,
            patch_content=record.patch_content,
            validation_profile_id=record.profile,
            expires_at=record.claim_expires_at,
        )

    def _expire_request(self, record: _ValidationRequest, now: datetime) -> None:
        if record.status in {
            ValidationRequestStatus.completed,
            ValidationRequestStatus.cancelled,
        }:
            return
        if record.expires_at <= now:
            record.status = ValidationRequestStatus.expired
            record.claimed_at = None
            record.claim_expires_at = None
        elif (
            record.status == ValidationRequestStatus.claimed
            and record.claim_expires_at is not None
            and record.claim_expires_at <= now
        ):
            record.status = ValidationRequestStatus.pending
            record.runner_id = None
            record.claimed_at = None
            record.claim_expires_at = None

    def _get_request(self, request_id: str) -> _ValidationRequest:
        try:
            return self._requests[request_id]
        except KeyError as exc:
            raise ValidationRequestNotFound("validation request not found") from exc

    def _redact(self, value: str) -> str:
        redacted = value
        secrets = list(self._redact_values)
        for runner in self._runners.values():
            secrets.append(runner.hmac_secret.decode("utf-8", errors="ignore"))
        for secret in secrets:
            if secret:
                redacted = redacted.replace(secret, "[REDACTED]")
        return redacted[: self._max_log_summary_chars]

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
