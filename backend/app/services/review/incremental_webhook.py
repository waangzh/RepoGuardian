"""GitHub pull_request synchronize webhook：为增量提交自动创建只读审查。

边界：
- 仅接受 HMAC 校验通过的 ``pull_request`` 事件，且 action 为 ``synchronize``。
- 只处理显式 allowlist 中的仓库；fork PR 的 head 仓库不自动审查。
- 不执行目标仓库代码；创建的是与手工审查相同的只读任务。
- 同一 delivery ID 幂等；重复投递返回已保存结果。
"""

from __future__ import annotations

import hashlib
import hmac
import re

from app.core.config import settings
from app.models.incremental import IncrementalWebhookReceipt
from app.review.contracts import ReviewRequest


def verify_signature(body: bytes, signature: str | None) -> bool:
    secret = settings.repoguardian_github_webhook_secret
    if (not secret or not isinstance(signature, str)
            or not re.fullmatch(r"sha256=[0-9a-f]{64}", signature)):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def allowed_repositories() -> set[str]:
    return {value.strip().casefold()
            for value in settings.repoguardian_incremental_review_webhook_repositories.split(",")
            if value.strip()}


def parse_synchronize(payload: dict) -> dict | None:
    """只提取创建审查所需的最小身份；不信任 payload 中的任意路径或命令。"""
    if not isinstance(payload, dict) or payload.get("action") != "synchronize":
        return None
    pr = payload.get("pull_request")
    repository = payload.get("repository")
    if not isinstance(pr, dict) or not isinstance(repository, dict):
        return None
    base = pr.get("base") or {}
    head = pr.get("head") or {}
    if not isinstance(base, dict) or not isinstance(head, dict):
        return None
    base_repo = base.get("repo") or {}
    head_repo = head.get("repo") or {}
    if not isinstance(base_repo, dict) or not isinstance(head_repo, dict):
        return None
    full_name = repository.get("full_name")
    number = pr.get("number")
    base_sha, head_sha = base.get("sha"), head.get("sha")
    if (not isinstance(full_name, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", full_name, re.ASCII)
            or type(number) is not int or number < 1
            or not isinstance(base_sha, str) or not isinstance(head_sha, str)
            or not re.fullmatch(r"[0-9a-f]{40}", base_sha)
            or not re.fullmatch(r"[0-9a-f]{40}", head_sha)
            or base_sha == head_sha):
        return None
    if (type(base_repo.get("id")) is not int or type(head_repo.get("id")) is not int
            or type(repository.get("id")) is not int):
        return None
    # fork PR 的 head 与 base 仓库不同；自动审查只覆盖仓库自身的分支。
    if base_repo["id"] != head_repo["id"] or base_repo["id"] != repository.get("id"):
        return None
    if pr.get("state") != "open" or pr.get("merged") is True or pr.get("draft") is True:
        return None
    if str(pr.get("html_url")).casefold() != f"https://github.com/{full_name}/pull/{number}".casefold():
        return None
    return {"repository": full_name, "pr_number": number, "pr_url": pr.get("html_url"),
            "base_sha": base_sha, "head_sha": head_sha}


class IncrementalWebhookService:
    def __init__(self, review_service, repository):
        self.review_service = review_service
        self.repository = repository

    async def handle(self, *, payload: dict, delivery_id: str | None) -> IncrementalWebhookReceipt:
        if not settings.repoguardian_incremental_review_webhook_enabled:
            return IncrementalWebhookReceipt(delivery_id=delivery_id or "unknown",
                                             status="skipped", reason="webhook_disabled")
        if not delivery_id:
            return IncrementalWebhookReceipt(delivery_id="missing", status="skipped",
                                             reason="delivery_id_missing")
        if len(delivery_id) > 128:
            return IncrementalWebhookReceipt(delivery_id="invalid", status="skipped",
                                             reason="delivery_id_invalid")
        parsed = parse_synchronize(payload)
        if parsed is None:
            return IncrementalWebhookReceipt(delivery_id=delivery_id, status="skipped",
                                             reason="unsupported_event")
        if parsed["repository"].casefold() not in allowed_repositories():
            return IncrementalWebhookReceipt(delivery_id=delivery_id, status="skipped",
                                             reason="repository_not_allowed")
        claimed = await self._claim(delivery_id, parsed)
        if not claimed:
            existing = await self._load(delivery_id)
            if existing is None:
                return IncrementalWebhookReceipt(delivery_id=delivery_id, status="skipped",
                                                 reason="duplicate_delivery")
            if any(existing.get(key) != parsed[key] for key in ("repository", "pr_number", "head_sha")):
                return IncrementalWebhookReceipt(delivery_id=delivery_id, status="failed",
                                                 reason="delivery_identity_conflict")
            status = existing["status"] if existing["status"] in {"accepted", "completed", "failed"} else "skipped"
            return IncrementalWebhookReceipt(delivery_id=delivery_id, status=status,
                                             reason=existing.get("reason"),
                                             task_id=existing.get("task_id"))
        try:
            task = await self._create(parsed, delivery_id)
        except ValueError as exc:
            reason = ("webhook_snapshot_changed" if str(exc) == "webhook_snapshot_changed"
                      else "review_creation_rejected")
            await self._finish(delivery_id, status="failed", reason=reason)
            return IncrementalWebhookReceipt(delivery_id=delivery_id, status="failed", reason=reason)
        except Exception as exc:
            await self._finish(delivery_id, status="failed", reason=type(exc).__name__)
            raise
        await self._finish(delivery_id, status="completed", task_id=task.id,
                           reason="review_created")
        return IncrementalWebhookReceipt(delivery_id=delivery_id, status="completed",
                                         task_id=task.id, reason="review_created")

    async def _claim(self, delivery_id: str, parsed: dict) -> bool:
        import asyncio

        return await asyncio.to_thread(
            self.repository.claim_webhook_delivery, delivery_id=delivery_id,
            repository=parsed["repository"], pr_number=parsed["pr_number"],
            head_sha=parsed["head_sha"])

    async def _load(self, delivery_id: str) -> dict | None:
        import asyncio

        return await asyncio.to_thread(self.repository.get_webhook_delivery, delivery_id)

    async def _finish(self, delivery_id: str, *, status: str, task_id: str | None = None,
                      reason: str | None = None) -> None:
        import asyncio

        await asyncio.to_thread(self.repository.finish_webhook_delivery, delivery_id,
                                status=status, task_id=task_id, reason=reason)

    async def _create(self, parsed: dict, delivery_id: str):
        service = self.review_service
        pr_url = parsed["pr_url"]
        if not isinstance(pr_url, str) or not pr_url.startswith("https://github.com/"):
            raise ValueError("unsupported_pr_url")
        return await service.create(ReviewRequest(pr_url=pr_url),
            f"webhook:{delivery_id}", caller_id="github-webhook",
            expected_snapshot=(parsed["base_sha"], parsed["head_sha"]))
