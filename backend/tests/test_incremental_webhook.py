"""增量审查 webhook：签名、allowlist、fork 拒绝与投递幂等。"""

import hashlib
import hmac
import json
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import settings
from app.main import app
from app.services.review.incremental_webhook import parse_synchronize, verify_signature
from tests.support.persistence import persistence as persistence


def payload(**overrides):
    value = {
        "action": "synchronize",
        "repository": {"id": 7, "full_name": "acme/widgets"},
        "pull_request": {
            "number": 3, "state": "open", "merged": False, "draft": False,
            "html_url": "https://github.com/acme/widgets/pull/3",
            "base": {"sha": "a" * 40, "repo": {"id": 7}},
            "head": {"sha": "b" * 40, "repo": {"id": 7}},
        },
    }
    value.update(overrides)
    return value


def test_signature_requires_configured_secret_and_exact_hmac(monkeypatch):
    body = b'{"action":"synchronize"}'
    monkeypatch.setattr(settings, "repoguardian_github_webhook_secret", None)
    assert verify_signature(body, "sha256=whatever") is False
    monkeypatch.setattr(settings, "repoguardian_github_webhook_secret", "secret")
    expected = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_signature(body, expected) is True
    assert verify_signature(body, expected[:-1] + "0") is False
    assert verify_signature(body, None) is False


@pytest.mark.parametrize("overrides,expected", [
    ({}, True),
    ({"action": "opened"}, False),
    ({"pull_request": {**payload()["pull_request"], "draft": True}}, False),
    ({"pull_request": {**payload()["pull_request"], "state": "closed"}}, False),
    ({"pull_request": {**payload()["pull_request"], "merged": True}}, False),
    ({"pull_request": {**payload()["pull_request"],
                       "head": {"sha": "b" * 40, "repo": {"id": 9}}}}, False),
    ({"repository": {"id": 9, "full_name": "acme/widgets"}}, False),
    ({"pull_request": {**payload()["pull_request"], "base": {"sha": "b" * 40, "repo": {"id": 7}}}}, False),
])
def test_only_open_in_repo_synchronize_events_are_accepted(overrides, expected):
    assert (parse_synchronize(payload(**overrides)) is not None) is expected


def test_parse_returns_minimal_identity_without_paths():
    parsed = parse_synchronize(payload())
    assert parsed == {"repository": "acme/widgets", "pr_number": 3,
                      "pr_url": "https://github.com/acme/widgets/pull/3",
                      "base_sha": "a" * 40, "head_sha": "b" * 40}


@pytest.mark.parametrize("pr_change", [
    {"base": []}, {"head": "invalid"}, {"base": {"repo": "invalid"}},
    {"html_url": "https://github.com/other/repo/pull/3"},
    {"head": {"sha": "--bad", "repo": {"id": 7}}},
    {"number": True},
])
def test_malformed_or_mismatched_pr_identity_is_rejected(pr_change):
    assert parse_synchronize(payload(
        pull_request={**payload()["pull_request"], **pr_change})) is None


@pytest.mark.parametrize("changed", ["head_sha", "declared_base_sha"])
async def test_webhook_does_not_create_task_for_a_different_snapshot(changed):
    from unittest.mock import AsyncMock
    from app.services.review.incremental_webhook import IncrementalWebhookService

    snapshot = {"head_sha": "b" * 40, "declared_base_sha": "a" * 40,
                "effective_base_sha": "a" * 40}
    snapshot[changed] = "e" * 40
    service = SimpleNamespace(
        preview=AsyncMock(return_value=SimpleNamespace(
            preview_id="p", plan_hash="h" * 64, snapshot=SimpleNamespace(**snapshot))),
        create_confirmed_task=AsyncMock())
    with pytest.raises(ValueError, match="webhook_snapshot_changed"):
        await IncrementalWebhookService(service, None)._create(parse_synchronize(payload()))
    service.create_confirmed_task.assert_not_awaited()


def test_delivery_claim_and_receipt_are_persisted(persistence):
    repository = persistence[0]
    identity = {"delivery_id": "test-delivery", "repository": "acme/widgets",
                "pr_number": 3, "head_sha": "b" * 40}
    assert repository.claim_webhook_delivery(**identity)
    assert not repository.claim_webhook_delivery(**identity)
    repository.finish_webhook_delivery("test-delivery", status="completed", task_id="c" * 32)
    assert repository.get_webhook_delivery("test-delivery") == {
        **identity, "status": "completed", "task_id": "c" * 32, "reason": None}


@pytest.mark.asyncio
async def test_webhook_rejects_missing_secret_and_bad_signature(monkeypatch):
    body = json.dumps(payload()).encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        monkeypatch.setattr(settings, "repoguardian_github_webhook_secret", None)
        response = await client.post("/api/incremental/webhook", content=body,
                                     headers={"X-GitHub-Event": "pull_request"})
        assert response.status_code == 503
        monkeypatch.setattr(settings, "repoguardian_github_webhook_secret", "secret")
        response = await client.post("/api/incremental/webhook", content=body,
                                     headers={"X-GitHub-Event": "pull_request",
                                              "X-Hub-Signature-256": "sha256=bad"})
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_webhook_skips_disabled_or_unlisted_repository(monkeypatch):
    body = json.dumps(payload()).encode()
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    monkeypatch.setattr(settings, "repoguardian_github_webhook_secret", "secret")
    monkeypatch.setattr(settings, "repoguardian_incremental_review_webhook_enabled", False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/incremental/webhook", content=body, headers={
            "X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d-1",
            "X-Hub-Signature-256": signature})
        assert response.status_code == 200
        assert response.json()["reason"] == "webhook_disabled"
    monkeypatch.setattr(settings, "repoguardian_incremental_review_webhook_enabled", True)
    monkeypatch.setattr(settings, "repoguardian_incremental_review_webhook_repositories", "other/repo")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/incremental/webhook", content=body, headers={
            "X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d-2",
            "X-Hub-Signature-256": signature})
        assert response.status_code == 200
        assert response.json()["reason"] == "repository_not_allowed"


@pytest.mark.asyncio
async def test_duplicate_delivery_replays_saved_receipt(monkeypatch):
    from app.api import incremental as incremental_api

    class Repository:
        def __init__(self):
            self.records = {}

        def claim_webhook_delivery(self, *, delivery_id, repository, pr_number, head_sha):
            if delivery_id in self.records:
                return False
            self.records[delivery_id] = {"delivery_id": delivery_id, "repository": repository,
                "pr_number": pr_number, "head_sha": head_sha, "status": "accepted",
                "task_id": None, "reason": None}
            return True

        def finish_webhook_delivery(self, delivery_id, *, status, task_id=None, reason=None):
            self.records[delivery_id].update(status=status, task_id=task_id, reason=reason)

        def get_webhook_delivery(self, delivery_id):
            return self.records.get(delivery_id)

    created = []

    class Service:
        repository = Repository()

        async def preview(self, request):
            return SimpleNamespace(preview_id="p", plan_hash="h" * 64,
                                   snapshot=SimpleNamespace(head_sha="b" * 40, effective_base_sha="a" * 40,
                                                            declared_base_sha="a" * 40))

        async def create_confirmed_task(self, request):
            created.append(request)
            return SimpleNamespace(id="c" * 32, incremental={"version": "incremental-review-v1"})

    service = Service()
    monkeypatch.setattr(incremental_api, "get_review_service", lambda: service)
    monkeypatch.setattr(settings, "repoguardian_github_webhook_secret", "secret")
    monkeypatch.setattr(settings, "repoguardian_incremental_review_webhook_enabled", True)
    monkeypatch.setattr(settings, "repoguardian_incremental_review_webhook_repositories", "acme/widgets")
    body = json.dumps(payload()).encode()
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    headers = {"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d-3",
               "X-Hub-Signature-256": signature}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/api/incremental/webhook", content=body, headers=headers)
        second = await client.post("/api/incremental/webhook", content=body, headers=headers)
    assert first.json()["status"] == "completed" and first.json()["task_id"] == "c" * 32
    assert second.json()["status"] == "completed" and second.json()["task_id"] == "c" * 32
    assert len(created) == 1
