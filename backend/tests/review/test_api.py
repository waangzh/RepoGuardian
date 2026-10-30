import shutil
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.review.contracts import ReviewRequest
from app.services.review.review_service import ReviewService
from app.tools.diff_parser import DiffParser
from app.tools.git_tool import GitTool
from tests.review.test_orchestrator import ReviewModel
from tests.review.vnext_helpers import setup_source_run


@pytest.fixture
def service_pipeline(tmp_path, monkeypatch):
    from app.core.config import settings
    from app.api import reviews
    store, source_run, _, source_provider, _ = setup_source_run(tmp_path)
    source_run.pr.repository_id = 1
    workdir = tmp_path / "workspaces"
    workdir.mkdir()

    class FixedGit(GitTool):
        def clone_and_diff(self, pr, **kwargs):
            target = self._workdir / ("fixture-" + uuid4().hex)
            shutil.copytree(source_provider.evidence.repo_path, target)
            _, diff = self.unified_diff(target, pr.base.sha, pr.head.sha)
            return target, diff

    class GitHub:
        calls = 0
        async def fetch_pr(self, url):
            self.calls += 1
            return source_run.pr.model_copy(deep=True)

    github = GitHub()
    model = ReviewModel()
    provider = SimpleNamespace(_provider_name="test", _base_url="http://localhost",
        _default_model="fake", agent_model=lambda *args, **kwargs: model)
    monkeypatch.setattr(settings, "repoguardian_pr_discussions_enabled", False)
    monkeypatch.setattr(settings, "repoguardian_prepared_workspace_max_entries", 0)
    service = ReviewService(github, FixedGit(workdir), DiffParser(), provider, store,
                            source_provider.evidence.artifacts)
    monkeypatch.setattr(service, "start_worker", lambda: None)
    monkeypatch.setattr(reviews, "get_review_service", lambda: service)
    app = FastAPI()
    app.include_router(reviews.router, prefix="/api")
    return service, github, app


async def test_direct_create_is_idempotent_and_complete_report_uses_new_api(service_pipeline):
    service, github, app = service_pipeline
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        body = {"pr_url": "https://github.com/o/r/pull/1"}
        first = await client.post("/api/reviews", json=body, headers={"Idempotency-Key": "create"})
        assert first.status_code == 202, first.text
        run_id = first.json()["id"]
        second = await client.post("/api/reviews", json=body, headers={"Idempotency-Key": "create"})
        assert second.json()["id"] == run_id
        assert github.calls == 1
        changed = await client.post("/api/reviews", json={**body, "depth": "deep"},
                                    headers={"Idempotency-Key": "create"})
        assert changed.status_code == 409
        assert changed.json()["detail"]["code"] == "idempotency_key_conflict"
        assert (await client.post("/api/reviews", json=body)).status_code == 422
        assert await service._worker.run_once()
        result = (await client.get(f"/api/reviews/{run_id}")).json()
        assert result["status"] == "completed", result
        assert result["report"]["overview"]["status"] == "ready"
        assert len(result["report"]["findings"]) == 1
        assert "goals" not in result["report"]
        report = (await client.get(f"/api/reviews/{run_id}/report")).json()
        assert "更新比例计算逻辑" in report["markdown"]
        plan = (await client.get(f"/api/reviews/{run_id}/publication-plan")).json()
        assert plan["report_version"] == report["report"]["version"]
        events = await client.get(f"/api/reviews/{run_id}/events")
        assert "event: done" in events.text
        assert len((await client.get(f"/api/reviews/{run_id}/units")).json()) == 2
        assert (await client.post(f"/api/reviews/{run_id}/resume")).status_code == 404
        finding = result["report"]["findings"][0]["id"]
        annotated = await client.patch(f"/api/reviews/{run_id}/findings/{finding}/disposition",
            json={"status": "resolved", "note": "作者确认后续提交修复"})
        assert annotated.status_code == 200
        assert annotated.json()["dispositions"][finding]["status"] == "resolved"
        assert annotated.json()["findings"][0]["publishable"] is True


async def test_overview_retry_only_calls_overview_and_changes_report_version(service_pipeline):
    service, _, _ = service_pipeline
    run = await service.create(ReviewRequest(pr_url="https://github.com/o/r/pull/1"), "first")
    assert await service._worker.run_once()
    before = service.store.get(run.id)
    assert before.report.usage["model_calls"] == 4
    await service.retry(run.id, overview=True)
    assert await service._worker.run_once()
    after = service.store.get(run.id)
    assert after.status == "completed"
    assert after.report.usage["model_calls"] == 5
    assert after.report.version != before.report.version
    assert after.report.findings == before.report.findings
    assert after.hard_deadline == before.hard_deadline


async def test_optional_preview_does_not_create_a_run_or_call_model(service_pipeline):
    service, _, _ = service_pipeline
    before = service.store.list_runs()["total"]
    preview = await service.preview(ReviewRequest(pr_url="https://github.com/o/r/pull/1"))
    assert preview["plan"].units
    assert service.store.list_runs()["total"] == before


async def test_cancel_preserves_candidate_facts_and_rejects_retry(service_pipeline):
    service, _, app = service_pipeline
    run = await service.create(ReviewRequest(pr_url="https://github.com/o/r/pull/1"), "cancel")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post(f"/api/reviews/{run.id}/cancel")).status_code == 202
        current = (await client.get(f"/api/reviews/{run.id}")).json()
        assert current["status"] == "cancelled"
        assert current["report"]["overview"]["status"] == "skipped_cancelled"
        assert (await client.post(f"/api/reviews/{run.id}/retry")).status_code == 409
