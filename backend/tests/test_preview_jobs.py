import asyncio
import copy
import importlib.util
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, inspect, select, text

from app.api import reviews
from app.main import app
from app.models.orm import PreviewInputCacheOrm, PreviewJobOrm, ReviewPreviewOrm, utcnow
from app.models.review import ReviewPreviewRequest
from app.services.preview_input_cache import PreviewInputCache, input_cache_key
from app.services.preview_jobs import PreviewJobs, PreviewJobStore, PreviewLeaseLost
from test_review_plans import plan_service as plan_service, confirmation
from test_task_runtime_p4 import production_input as production_input
from test_phase6a_persistence import persistence as persistence

REQUEST = ReviewPreviewRequest(pr_url="https://github.com/o/r/pull/1")


def test_idempotency_restart_claim_cancel_and_old_owner_rejected(persistence):
    repository, _, sessions = persistence
    store = PreviewJobStore(repository)
    store.create("a" * 32, REQUEST)
    store.create("a" * 32, REQUEST)
    with pytest.raises(ValueError, match="conflict"):
        store.create("a" * 32, REQUEST.model_copy(update={"model": "another"}))
    assert store.claim("old")[0] == "a" * 32
    store.progress("a" * 32, "old", "clone", "正在克隆", percent=23)
    assert store.claim("new") is None
    with sessions.begin() as session:
        session.get(PreviewJobOrm, "a" * 32).lease_until = utcnow() - timedelta(seconds=1)
    assert not store.heartbeat("a" * 32, "old")
    restarted = PreviewJobStore(repository)
    assert restarted.claim("new")[0] == "a" * 32
    with pytest.raises(PreviewLeaseLost):
        store.progress("a" * 32, "old", "planning", "旧写者")
    assert not store.finish("a" * 32, "old", "completed", preview_id="stale")
    assert restarted.get("a" * 32).stages[-1].phase == "recovery"
    restarted.cancel("a" * 32)
    assert restarted.get("a" * 32).status == "cancelled"
    assert not restarted.finish("a" * 32, "new", "completed")
    assert restarted.claim("other") is None


async def test_worker_stores_progress_restores_result_expiry_and_consumption(plan_service):
    service, _, _, calls, _ = plan_service
    jobs = PreviewJobs(service)
    jobs.store.create("b" * 32, REQUEST)
    identity, request = jobs.store.claim(jobs.owner)
    await jobs.execute(identity, request)
    recovered = PreviewJobStore(service._repository).get(identity)
    assert recovered.status == "completed" and recovered.result.changed_file_count > 0
    assert {s.phase for s in recovered.stages} >= {"github", "clone", "index_files", "index_symbols", "index_relationships", "planning", "cleanup"}
    assert all(s.finished_at is not None for s in recovered.stages)
    assert calls == []
    with service._repository._session_factory.begin() as session:
        row = session.get(ReviewPreviewOrm, recovered.result.preview_id)
        payload = copy.deepcopy(row.payload)
        payload["response"]["expires_at"] = (utcnow() - timedelta(seconds=1)).isoformat()
        row.payload = payload
    assert jobs.store.get(identity).status == "expired"
    with service._repository._session_factory.begin() as session:
        session.get(ReviewPreviewOrm, recovered.result.preview_id).consumed_task_id = "created-task"
    assert jobs.store.get(identity).consumed_task_id == "created-task"
    assert jobs.store.get(identity).status == "consumed"


async def test_failure_is_persisted_and_does_not_leak_upstream_text(plan_service):
    service, *_ = plan_service
    service.preview = AsyncMock(side_effect=RuntimeError("token=do-not-expose"))
    jobs = PreviewJobs(service)
    jobs.store.create("c" * 32, REQUEST)
    await jobs.execute(*jobs.store.claim(jobs.owner))
    value = jobs.store.get("c" * 32)
    assert value.status == "failed" and value.result is None
    assert "do-not-expose" not in value.model_dump_json()
    assert "RuntimeError" not in value.error


async def test_shutdown_requeues_and_signals_thread_cancellation(plan_service):
    service, *_ = plan_service
    entered = asyncio.Event()
    events = []
    async def slow(*a, cancel_event=None, **k):
        events.append(cancel_event)
        entered.set()
        await asyncio.Event().wait()
    service.preview = slow
    jobs = PreviewJobs(service)
    jobs.store.create("d" * 32, REQUEST)
    running = asyncio.create_task(jobs.execute(*jobs.store.claim(jobs.owner)))
    await entered.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert events[0].is_set()
    assert jobs.store.get("d" * 32).status == "queued"
    assert jobs.store.claim("restarted")[0] == "d" * 32


async def test_repeated_preview_and_confirmation_reuse_inputs_but_refresh_metadata(plan_service):
    service, _, _, calls, _ = plan_service
    clone = service._git_tool.clone_and_diff = Mock(wraps=service._git_tool.clone_and_diff)
    first = await service.preview(REQUEST)
    second = await service.preview(REQUEST)
    assert first.plan_hash == second.plan_hash
    assert clone.call_count == 1
    await service.create_confirmed_task(confirmation(second))
    assert clone.call_count == 1
    assert service._github_tool.fetch_pr.await_count == 3
    assert calls == []


async def test_bad_or_expired_cache_rebuilds_and_changed_identity_misses(plan_service):
    service, _, _, _, pr = plan_service
    await service.preview(REQUEST)
    clone = service._git_tool.clone_and_diff = Mock(wraps=service._git_tool.clone_and_diff)
    sessions = service._repository._session_factory
    with sessions.begin() as session:
        row = session.scalar(select(PreviewInputCacheOrm))
        key = row.key
        row.digest = "corrupt"
    await service.preview(REQUEST)
    assert clone.call_count == 1
    with sessions.begin() as session:
        session.get(PreviewInputCacheOrm, key).expires_at = utcnow() - timedelta(seconds=1)
    await service.preview(REQUEST)
    assert clone.call_count == 2
    from app.graph.nodes.task_review_runtime import task_runtime_configuration
    configuration = task_runtime_configuration()
    assert input_cache_key(pr, configuration) != input_cache_key(
        pr.model_copy(update={"head": pr.head.model_copy(update={"sha": "f" * 40})}), configuration)
    assert input_cache_key(pr, configuration) != input_cache_key(pr,
        {**configuration, "call_resolution_version": "changed"})


def test_cache_capacity_and_oversize_are_bounded(persistence, monkeypatch):
    cache = PreviewInputCache(persistence[0])
    payload = {"diff": "", "index": {"file_index": [], "symbol_index": [], "repository_graph": {}, "project_meta": {}}, "diff_context": {}}
    for n in range(7):
        cache.put(str(n), payload)
    with persistence[2]() as session:
        assert len(list(session.scalars(select(PreviewInputCacheOrm)))) == 4
    monkeypatch.setattr("app.services.preview_input_cache.MAX_BYTES", 1)
    cache.put("oversize", payload)
    assert cache.get("oversize") is None


def test_large_repetitive_index_is_compressed_and_expansion_is_bounded(persistence, monkeypatch):
    cache = PreviewInputCache(persistence[0])
    monkeypatch.setattr("app.services.preview_input_cache.MAX_BYTES", 4096)
    payload = {"diff": "source text\n" * 20000,
        "index": {"file_index": [], "symbol_index": [], "repository_graph": {}, "project_meta": {}},
        "diff_context": {}}
    cache.put("large", payload)
    assert cache.get("large") == payload
    monkeypatch.setattr("app.services.preview_input_cache.MAX_RAW_BYTES", 1000)
    assert cache.get("large") is None
    cache.put("too-large-expanded", payload)
    assert cache.get("too-large-expanded") is None


async def test_formal_review_reuses_index_only_for_matching_actual_diff_and_snapshot(plan_service, monkeypatch):
    from langgraph.runtime import Runtime
    from app.graph.nodes.repo_index import repo_index_node
    from app.tools.repo_indexer import RepoIndexer
    service, state, _, _, pr = plan_service
    await service.preview(REQUEST)
    from app.graph.nodes.task_review_runtime import task_runtime_configuration
    state = {**state, "pr_info": pr.model_dump(mode="json"),
             "task_runtime_configuration": task_runtime_configuration()}
    state["diff_text"] = service._git_tool.clone_and_diff()[1]
    runtime = Runtime(context={"coordination_repository": service._repository})
    scan = AsyncMock(side_effect=AssertionError("matching snapshot should reuse index"))
    monkeypatch.setattr(RepoIndexer, "execute", scan)
    reused = await repo_index_node(state, runtime)
    assert reused["symbol_index"] and "已复用索引" in reused["step_progress"][-1]["message"]
    assert scan.await_count == 0
    scan.side_effect = None
    scan.return_value = {key: reused[key] for key in ("file_index", "symbol_index", "repository_graph", "project_meta")}
    for changes in ({"diff_text": "different diff"}, {"effective_base_sha": "f" * 40}, {"head_sha": "f" * 40}):
        await repo_index_node({**state, **changes}, runtime)
    assert scan.await_count == 3


async def test_async_api_accepts_idempotent_job_and_returns_recoverable_state(plan_service, monkeypatch):
    service, *_ = plan_service
    jobs = PreviewJobs(service)
    jobs.start = lambda: None
    monkeypatch.setattr(reviews, "get_preview_jobs", lambda: jobs)
    payload = {"id": "e" * 32, "request": REQUEST.model_dump(mode="json")}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for _ in range(2):
            response = await client.post("/api/reviews/preview-jobs", json=payload)
            assert response.status_code == 202 and response.json()["status"] == "queued"
        assert (await client.get("/api/reviews/preview-jobs/" + "e" * 32)).json()["request"]["pr_url"] == str(REQUEST.pr_url)
        payload["request"]["model"] = "different"
        assert (await client.post("/api/reviews/preview-jobs", json=payload)).status_code == 409
        assert (await client.post("/api/reviews/preview-jobs", json={**payload, "id": "../outside"})).status_code == 422
        assert (await client.get("/api/reviews/preview-jobs/missing")).status_code == 404
        assert (await client.post("/api/reviews/preview-jobs/" + "e" * 32 + "/cancel")).json()["status"] == "cancelled"


def test_migration_preserves_existing_previews_in_both_directions(tmp_path):
    path = Path(__file__).parents[1] / "alembic/versions/20261017_0006_preview_jobs.py"
    spec = importlib.util.spec_from_file_location("preview_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE review_previews (id TEXT PRIMARY KEY, payload JSON)"))
        connection.execute(text("INSERT INTO review_previews VALUES ('old', :payload)"), {"payload": json.dumps({"preserved": True})})
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert {"preview_jobs", "preview_input_cache"} <= set(inspect(connection).get_table_names())
            migration.downgrade()
            assert connection.execute(text("SELECT payload FROM review_previews WHERE id='old'")).scalar() == '{"preserved": true}'
    engine.dispose()
