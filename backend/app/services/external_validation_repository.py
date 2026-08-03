"""外部验证协调状态仓储。

只保存协议恢复所需状态；不会执行仓库命令，也不会向 GitHub 写入代码。
"""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, select

from app.core.database import sync_session
from app.models.orm import (
    ProjectCIRequestOrm,
    ProjectCIWebhookDeliveryOrm,
    RunnerRegistrationOrm,
    RunnerResultIdempotencyOrm,
    UserValidationRequestOrm,
    utcnow,
)
from app.models.runner import RunnerRegistration


class RunnerCredentialDecryptionError(RuntimeError):
    """持久化 Runner 凭据无法由当前服务端密钥解密。"""


class RunnerCredentialCipher:
    """使用稳定的服务端管理密钥保护 Runner HMAC secret。"""

    def __init__(self, server_secret: str) -> None:
        if not server_secret:
            raise ValueError("runner credential encryption requires a server secret")
        key = base64.urlsafe_b64encode(hashlib.sha256(server_secret.encode("utf-8")).digest())
        self._fernet = Fernet(key)

    def encrypt(self, value: bytes) -> bytes:
        return self._fernet.encrypt(value)

    def decrypt(self, value: bytes) -> bytes:
        try:
            return self._fernet.decrypt(value)
        except InvalidToken as exc:
            raise RunnerCredentialDecryptionError(
                "stored runner credential cannot be decrypted with the current admin token"
            ) from exc


class ExternalValidationRepository:
    def __init__(self, session_factory=sync_session) -> None:
        self._session_factory = session_factory

    def save_runner(
        self,
        registration: RunnerRegistration,
        *,
        api_token_hash: bytes,
        encrypted_hmac_secret: bytes,
    ) -> None:
        with self._session_factory.begin() as session:
            row = session.get(RunnerRegistrationOrm, registration.runner_id)
            if row is None:
                row = RunnerRegistrationOrm(runner_id=registration.runner_id)
                session.add(row)
            row.display_name = registration.display_name
            row.public_key = registration.public_key
            row.allowed_repositories = list(registration.allowed_repositories)
            row.allowed_profiles = list(registration.allowed_profiles)
            row.enabled = registration.enabled
            row.api_token_hash = api_token_hash
            row.encrypted_hmac_secret = encrypted_hmac_secret
            row.updated_at = utcnow()

    def load_runners(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = session.scalars(select(RunnerRegistrationOrm)).all()
            return [
                {
                    "public": RunnerRegistration(
                        runner_id=row.runner_id,
                        display_name=row.display_name,
                        public_key=row.public_key,
                        allowed_repositories=list(row.allowed_repositories),
                        allowed_profiles=list(row.allowed_profiles),
                        enabled=row.enabled,
                    ),
                    "api_token_hash": bytes(row.api_token_hash),
                    "encrypted_hmac_secret": bytes(row.encrypted_hmac_secret),
                    "last_seen_at": row.last_seen_at,
                }
                for row in rows
            ]

    def touch_runner(self, runner_id: str, seen_at: datetime) -> None:
        with self._session_factory.begin() as session:
            row = session.get(RunnerRegistrationOrm, runner_id)
            if row is not None:
                row.last_seen_at = seen_at
                row.updated_at = utcnow()

    def save_user_request(self, payload: dict[str, Any]) -> None:
        with self._session_factory.begin() as session:
            self._upsert_user_request(session, payload)

    def save_user_result(
        self,
        request_payload: dict[str, Any],
        *,
        runner_id: str,
        idempotency_key: str,
        fingerprint: bytes,
        receipt: dict[str, Any],
    ) -> None:
        with self._session_factory.begin() as session:
            self._upsert_user_request(session, request_payload)
            existing = session.scalar(
                select(RunnerResultIdempotencyOrm).where(
                    RunnerResultIdempotencyOrm.runner_id == runner_id,
                    RunnerResultIdempotencyOrm.idempotency_key == idempotency_key,
                )
            )
            if existing is None:
                session.add(RunnerResultIdempotencyOrm(
                    runner_id=runner_id,
                    idempotency_key=idempotency_key,
                    request_id=request_payload["request_id"],
                    fingerprint=fingerprint,
                    receipt=receipt,
                ))

    def load_user_requests(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            return [self._user_request_payload(row) for row in session.scalars(
                select(UserValidationRequestOrm)
            )]

    def load_runner_result_idempotency(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = session.scalars(select(RunnerResultIdempotencyOrm)).all()
            return [
                {
                    "runner_id": row.runner_id,
                    "idempotency_key": row.idempotency_key,
                    "fingerprint": bytes(row.fingerprint),
                    "receipt": dict(row.receipt),
                }
                for row in rows
            ]

    def save_project_ci_request(
        self,
        *,
        summary: dict[str, Any],
        workflow: dict[str, Any] | None,
        result: dict[str, Any] | None,
    ) -> None:
        with self._session_factory.begin() as session:
            request_id = summary["request_id"]
            row = session.get(ProjectCIRequestOrm, request_id)
            if row is None:
                row = ProjectCIRequestOrm(
                    request_id=request_id,
                    task_id=summary["task_id"],
                    patch_id=summary["patch_id"],
                    repository=summary["repository"],
                    status=summary["status"],
                    summary=summary,
                    expires_at=_datetime(summary["expires_at"]),
                    created_at=_datetime(summary["created_at"]),
                    updated_at=_datetime(summary["updated_at"]),
                )
                session.add(row)
            row.status = summary["status"]
            row.summary = summary
            row.workflow = workflow
            row.result = result
            row.run_id = summary.get("run_id")
            row.expires_at = _datetime(summary["expires_at"])
            row.updated_at = _datetime(summary["updated_at"])

    def load_project_ci_requests(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = session.scalars(select(ProjectCIRequestOrm)).all()
            return [
                {
                    "summary": dict(row.summary),
                    "workflow": dict(row.workflow) if row.workflow else None,
                    "result": dict(row.result) if row.result else None,
                }
                for row in rows
            ]

    def record_project_ci_delivery(self, delivery_id: str, request_id: str) -> None:
        with self._session_factory.begin() as session:
            if session.get(ProjectCIWebhookDeliveryOrm, delivery_id) is None:
                session.add(ProjectCIWebhookDeliveryOrm(
                    delivery_id=delivery_id,
                    request_id=request_id,
                ))

    def load_project_ci_deliveries(self) -> set[str]:
        with self._session_factory() as session:
            return set(session.scalars(select(ProjectCIWebhookDeliveryOrm.delivery_id)).all())

    def delete_project_ci_request(self, request_id: str) -> None:
        with self._session_factory.begin() as session:
            session.execute(
                delete(ProjectCIRequestOrm).where(ProjectCIRequestOrm.request_id == request_id)
            )

    @staticmethod
    def _upsert_user_request(session, payload: dict[str, Any]) -> None:  # type: ignore[no-untyped-def]
        row = session.get(UserValidationRequestOrm, payload["request_id"])
        if row is None:
            row = UserValidationRequestOrm(
                request_id=payload["request_id"],
                task_id=payload["task_id"],
                patch_id=payload["patch_id"],
                repository_id=payload["repository_id"],
                clone_url=payload["clone_url"],
                base_sha=payload["base_sha"],
                head_sha=payload["head_sha"],
                patch_sha=payload["patch_sha"],
                patch_content=payload["patch_content"],
                profile=payload["profile"],
                status=payload["status"],
                created_at=_datetime(payload["created_at"]),
                expires_at=_datetime(payload["expires_at"]),
            )
            session.add(row)
        row.fetch_ref = payload.get("fetch_ref")
        row.status = payload["status"]
        row.runner_id = payload.get("runner_id")
        row.claimed_at = _optional_datetime(payload.get("claimed_at"))
        row.claim_expires_at = _optional_datetime(payload.get("claim_expires_at"))
        row.result = payload.get("result")
        row.updated_at = utcnow()

    @staticmethod
    def _user_request_payload(row: UserValidationRequestOrm) -> dict[str, Any]:
        return {
            "request_id": row.request_id,
            "task_id": row.task_id,
            "patch_id": row.patch_id,
            "repository_id": row.repository_id,
            "clone_url": row.clone_url,
            "fetch_ref": row.fetch_ref,
            "base_sha": row.base_sha,
            "head_sha": row.head_sha,
            "patch_sha": row.patch_sha,
            "patch_content": row.patch_content,
            "profile": row.profile,
            "status": row.status,
            "runner_id": row.runner_id,
            "result": dict(row.result) if row.result else None,
            "created_at": _datetime(row.created_at),
            "expires_at": _datetime(row.expires_at),
            "claimed_at": _optional_datetime(row.claimed_at),
            "claim_expires_at": _optional_datetime(row.claim_expires_at),
        }


def _datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _optional_datetime(value: datetime | str | None) -> datetime | None:
    return _datetime(value) if value is not None else None
