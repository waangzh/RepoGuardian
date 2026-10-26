import json

import httpx
import pytest
from sqlalchemy import inspect

from app.main import app
from app.api import reviews
from app.core.database import Base
from app.review.contracts import ReviewCreateRequest
from app.services.preview.preview_jobs import PreviewJobStore
from tests.review.test_native_lifecycle import preview_and_create

pytest_plugins = ["tests.review.test_native_lifecycle"]


async def test_current_api_pipeline_findings_report_and_frozen_context(pipeline, monkeypatch):
    service, repository, _, pr, _ = pipeline
    monkeypatch.setattr(reviews, "get_review_service", lambda: service)
    monkeypatch.setattr(reviews, "GitHubTool", lambda token=None: service.github_tool)
    _, _, task = await preview_and_create(service)
    assert await service._worker.run_once()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/reviews/{task.id}")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["review_report"]["complete"] and payload["status"] == "completed"
        assert not {"engine", "task_runtime_report", "task_runtime_configuration"} & payload.keys()
        for suffix in ("findings", "publication-plan", "publication-summary", "report"):
            response = await client.get(f"/api/reviews/{task.id}/{suffix}")
            assert response.status_code == 200, (suffix, response.text)
        findings = (await client.get(f"/api/reviews/{task.id}/findings")).json()["items"]
        assert len(findings) == 1 and findings[0]["review_status"] == "confirmed"
        response = await client.patch(f"/api/reviews/{task.id}/findings/{findings[0]['id']}/disposition",
                                      json={"status": "ignored", "reason": "人工评估记录"})
        assert response.status_code == 200, response.text
        assert response.json()["items"][0]["review_status"] == "confirmed"
        assert response.json()["items"][0]["disposition"]["status"] == "ignored"
        preview = await client.post(f"/api/reviews/{task.id}/review-comment-preview",
                                     json={"detail": "compact", "exclude_resolved": False})
        assert preview.status_code == 200, preview.text
        assert preview.json()["preview_markdown"]
        checked = await client.post(f"/api/reviews/{task.id}/review-comment-check",
                                     json={"detail": "compact", "exclude_resolved": False})
        assert checked.status_code == 200 and checked.json()["status"] == "current"
        diff = await client.get(f"/api/reviews/{task.id}/files/0/diff")
        assert diff.status_code == 200
        assert diff.json()["snapshot"]["head_sha"] == task.snapshot.head_sha
        history = await client.get("/api/reviews?q=Change")
        assert history.status_code == 200 and history.json()["total"] == 1
        invalid = await client.post("/api/reviews", json={"pr_url": task.pr_url})
        assert invalid.status_code == 422
        capabilities = (await client.get("/api/system/review-capabilities")).json()
        assert "engine" not in capabilities and "plan_version" not in capabilities
        assert "model_calls" in capabilities["limits"]["defaults"]
        pr.head.sha = "0" * 40
        stale = await client.post(f"/api/reviews/{task.id}/review-comment-check",
                                   json={"detail": "compact", "exclude_resolved": False})
        assert stale.status_code == 200 and stale.json()["status"] == "stale"
        assert "旧 Head" in stale.json()["review_comment_markdown"]
    assert len(repository.finding_dispositions(task.id)) == 1


async def test_preview_job_store_replay_cancel_and_consumption(pipeline):
    service, _, _, _, _ = pipeline
    from app.review.contracts import ReviewPreviewRequest
    store = PreviewJobStore(service.repository)
    request = ReviewPreviewRequest(pr_url="https://github.com/local/repo/pull/1",
                                  plan={"include_pr_discussions": False})
    identity = "a" * 32
    store.create(identity, request)
    store.create(identity, request)
    with pytest.raises(ValueError, match="conflict"):
        store.create(identity, request.model_copy(update={"model": "other"}))
    claimed = store.claim("owner")
    assert claimed and claimed[0] == identity
    store.progress(identity, "owner", "clone", "正在准备", percent=25)
    store.cancel(identity)
    assert not store.finish(identity, "owner", "completed")
    assert store.get(identity).status == "cancelled"


def test_current_schema_has_no_internal_agent_sessions_or_historical_versions(pipeline):
    _, _, _, _, engine = pipeline
    tables = set(inspect(engine).get_table_names())
    assert not tables & {"review_units", "review_issues", "human_requests", "side_effects"}
    columns = {item["name"] for item in inspect(engine).get_columns("review_tasks")}
    assert not any("version" in name for name in columns)
    assert tables == set(Base.metadata.tables)
    schema = app.openapi()
    fields = schema["components"]["schemas"]["ReviewCreateRequest"]["properties"]
    assert set(fields) == set(ReviewCreateRequest.model_fields)
    assert "engine" not in json.dumps(schema)


def test_alembic_current_schema_matches_models_on_fresh_database(tmp_path):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine
    engine = create_engine(f"sqlite:///{(tmp_path / 'migrated.sqlite').as_posix()}")
    config = Config("alembic.ini")
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    assert set(inspect(engine).get_table_names()) == set(Base.metadata.tables) | {"alembic_version"}
    engine.dispose()
