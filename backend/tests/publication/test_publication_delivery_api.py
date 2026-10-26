import httpx
import pytest

from app.api import publications
from app.main import app
from app.services.publication.publication_service import PublicationService
from app.services.publication.publication_repository import PublicationRepository
from app.tools.github.github_publisher import GitHubPublisher
from tests.support.review_tasks import publishable_task as publishable_task
from tests.support.publication_fixtures import publish_config
from tests.publication.test_sticky_summary import FakeGitHub
from tests.support.persistence import persistence as persistence


@pytest.fixture
def publication_api(persistence, monkeypatch):
    review_repo, queue, sessions = persistence
    task = publishable_task()
    review_repo.create_task(task)
    fake, config = FakeGitHub(), publish_config()
    service = PublicationService(PublicationRepository(sessions), GitHubPublisher("fixture", transport=httpx.MockTransport(fake)),
                                 config, review_repo.get_task, queue)
    monkeypatch.setattr(publications, "settings", config)
    monkeypatch.setattr(publications, "get_publication_service", lambda: service)
    return task, service, fake, config


def api_client(*, host="127.0.0.1", authenticated=True):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(host, 1234)), base_url="http://127.0.0.1:8000",
        headers={"Authorization": "Bearer " + "x" * 32} if authenticated else {})


async def test_preflight_confirm_queue_status_and_idempotent_repeat(publication_api):
    task, service, fake, config = publication_api
    async with api_client() as client:
        preview = await client.post(f"/api/reviews/{task.id}/publication-preflight", json={})
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["summary_action"] == "create" and body["inline"]["ready"] == 1
        assert "context" not in body and "plan" not in body
        selection = {"expected_plan_hash": body["plan_hash"], "expected_head_sha": body["head_sha"], "finding_keys": body["finding_keys"]}
        first = await client.post(f"/api/reviews/{task.id}/publications", json=selection, headers={"Idempotency-Key": "one"})
        assert first.status_code == 202 and not fake.writes
        identity = first.json()["publication_id"]
        assert await service.worker.run_once()
        result = await client.get(first.json()["status_url"])
        assert result.status_code == 200 and result.json()["state"] == "succeeded"
        assert all(item["github_url"].startswith("https://github.com/") for item in result.json()["items"])
        fake.head = "d" * 40
        repeated = await client.post(f"/api/reviews/{task.id}/publications", json=selection, headers={"Idempotency-Key": "one"})
        assert repeated.status_code == 202 and repeated.json()["publication_id"] == identity
        selection["finding_keys"] = []
        conflict = await client.post(f"/api/reviews/{task.id}/publications", json=selection, headers={"Idempotency-Key": "one"})
        assert conflict.status_code == 409
        assert len((await client.get(f"/api/reviews/{task.id}/publications")).json()) == 1
        assert (await client.get(f"/api/reviews/other/publications/{identity}")).status_code == 404


async def test_operator_locality_origin_and_disabled_guards(publication_api):
    task, service, fake, config = publication_api
    for host, authenticated in [("127.0.0.1", False), ("203.0.113.1", True)]:
        async with api_client(host=host, authenticated=authenticated) as client:
            assert (await client.post(f"/api/reviews/{task.id}/publication-preflight", json={})).status_code == 403
    async with api_client() as client:
        assert (await client.post(f"/api/reviews/{task.id}/publication-preflight", json={}, headers={"Origin": "https://attacker.example"})).status_code == 403
        config.repoguardian_github_publish_enabled = False
        assert (await client.get(f"/api/reviews/{task.id}/publications")).status_code == 503
    assert not fake.writes


async def test_client_cannot_edit_markdown_or_bypass_partial_ack(publication_api):
    task, service, fake, config = publication_api
    async with api_client() as client:
        preview = (await client.post(f"/api/reviews/{task.id}/publication-preflight", json={"finding_keys": []})).json()
        assert preview["requires_partial_acknowledgement"]
        selection = {"expected_plan_hash": preview["plan_hash"], "expected_head_sha": preview["head_sha"], "finding_keys": []}
        url = f"/api/reviews/{task.id}/publications"
        assert (await client.post(url, json=selection, headers={"Idempotency-Key": "partial"})).status_code == 409
        assert (await client.post(url, json=selection | {"body": "unverified"}, headers={"Idempotency-Key": "edit"})).status_code == 422
        assert (await client.post(url, json=selection | {"publish_summary": False}, headers={"Idempotency-Key": "no-summary"})).status_code == 422
        assert (await client.post(url, json=selection)).status_code == 422
        selection["acknowledge_partial_review"] = True
        accepted = await client.post(url, json=selection, headers={"Idempotency-Key": "partial"})
        assert accepted.status_code == 202
        await service.worker.run_once()
        assert not fake.reviews and len(fake.comments) == 1
        assert "未选择：1" in next(iter(fake.comments.values()))["body"]
    assert service.repo.get(accepted.json()["publication_id"]).state == "succeeded"
