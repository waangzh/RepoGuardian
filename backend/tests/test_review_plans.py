import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

from app.api import reviews
from app.core.config import settings
from app.main import app
from app.models.review import ReviewCreateRequest, ReviewPreviewRequest, ReviewTask, TaskStatus, PullRequestInfo
from app.models.review_plan import ReviewPlanOptions, ReviewLimits, ReviewScope
from app.services.review_plan import normalize_plan
from app.services.review_service import ReviewService
from app.services.report_service import ReportService
from app.tools.git_tool import GitTool
from app.tools.diff_parser import DiffParser
from app.graph.nodes.task_review_runtime import task_review_runtime_node
from app.graph.nodes.review_units import review_plan_node
from app.services.review_run_budget import ReviewRunBudget, activate_run_budget
from test_phase6a_persistence import persistence as persistence
from test_task_runtime_p4 import production_input as production_input


def test_deadline_handles_sqlite_naive_utc_and_never_resets():
    from app.services.review_plan import remaining_review_seconds
    created = datetime.now(timezone.utc) - timedelta(seconds=120)
    for value in (created, created.replace(tzinfo=None), created.isoformat(), created.replace(tzinfo=None).isoformat()):
        assert remaining_review_seconds({"deadline_seconds": 60}, value) < -59


@pytest.mark.parametrize("payload", [
    {"depth": "unlimited"}, {"focus": []}, {"focus": ["shell"]},
    {"limits": {"max_tokens": -1}}, {"limits": {"max_model_calls": True}},
    {"scope": {"mode": "selected", "primary_files": ["../secret"]}},
    {"scope": {"mode": "selected", "primary_files": []}},
    {"scope": {"mode": "all", "primary_files": ["a.py"]}},
    {"include_pr_discussions": "false"}, {"include_pr_discussions": 1},
])
def test_invalid_plan_rejected(payload):
    with pytest.raises(ValidationError):
        ReviewPlanOptions.model_validate(payload)


def test_server_maximum_and_unknown_price_rejected(monkeypatch):
    monkeypatch.setattr(settings, "repoguardian_model_pricing_json", "{}")
    with pytest.raises(ValueError, match="maximum"):
        normalize_plan(ReviewPlanOptions(limits=ReviewLimits(max_tokens=settings.repoguardian_run_max_tokens + 1)), "fixture")
    with pytest.raises(ValueError, match="pricing"):
        normalize_plan(ReviewPlanOptions(limits=ReviewLimits(max_cost_microusd=1)), "fixture")


def test_discussion_policy_and_legacy_serialization(monkeypatch):
    from app.services.review_plan import configure_runtime
    assert "include_pr_discussions" not in ReviewPlanOptions().model_dump(mode="json")
    monkeypatch.setattr(settings, "repoguardian_pr_discussions_enabled", True)
    assert normalize_plan(None, "fixture").include_pr_discussions is True
    options = normalize_plan(ReviewPlanOptions(include_pr_discussions=False), "fixture")
    frozen = configure_runtime({"discussion_contract": "pr-discussion-context-v1"}, options, {}, "hash")
    assert frozen["discussion_contract"] is None
    assert frozen["effective_plan"]["include_pr_discussions"] is False
    monkeypatch.setattr(settings, "repoguardian_pr_discussions_enabled", False)
    assert normalize_plan(None, "fixture").include_pr_discussions is False
    with pytest.raises(ValueError, match="disabled by server policy"):
        normalize_plan(ReviewPlanOptions(include_pr_discussions=True), "fixture")


async def test_disabled_history_is_frozen_and_never_opens_discussion_store(plan_service, monkeypatch):
    from app.services.pr_discussions import prepare_discussions_node
    from app.services.pr_discussion_recheck import recheck_discussions_node
    service, _, _, calls, _ = plan_service
    preview = await service.preview(ReviewPreviewRequest(pr_url="https://github.com/o/r/pull/1",
        plan=ReviewPlanOptions(include_pr_discussions=False)))
    task = await service.create_confirmed_task(confirmation(preview))
    restored = service._repository.get_task(task.id)
    assert restored.confirmed_plan.include_pr_discussions is False
    assert restored.task_runtime_configuration["discussion_contract"] is None
    async def forbidden(*args, **kwargs):
        raise AssertionError("disabled history must not open store or access GitHub/model")
    monkeypatch.setattr("app.services.pr_discussions.open_store", forbidden)
    monkeypatch.setattr("app.services.pr_discussion_recheck.open_store", forbidden)
    state = {"task_runtime_configuration": restored.task_runtime_configuration}
    assert await prepare_discussions_node(state) == {}
    assert await recheck_discussions_node(state) == {}
    assert calls == []


@pytest.fixture
def plan_service(production_input, persistence, monkeypatch):
    state, provider, calls = production_input
    configuration = copy.deepcopy(state["task_runtime_configuration"])
    configuration["call_resolution_version"] = "import-evidence-v5"
    monkeypatch.setattr("app.graph.nodes.task_review_runtime.task_runtime_configuration", lambda *a, **k: copy.deepcopy(configuration))
    monkeypatch.setattr("app.services.review_service._cleanup_repo", AsyncMock(return_value=True))
    pr = PullRequestInfo(owner="o", repo="r", number=1, title="fixture", body="", clone_url="https://github.com/o/r.git",
        html_url="https://github.com/o/r/pull/1",
        head={"sha": state["head_sha"], "ref": "feature", "repo_clone_url": "https://github.com/o/r.git"},
        base={"sha": state["base_sha"], "ref": "main", "repo_clone_url": "https://github.com/o/r.git"})
    state["pr_info"] = pr.model_dump(mode="json")
    git = GitTool()
    raw = git._run([git._git, "-C", state["repo_path"], "diff", "--unified=3", state["base_sha"], state["head_sha"]])
    git.clone_and_diff = lambda *a, **k: (state["repo_path"], raw)
    repository, queue, _ = persistence
    service = ReviewService(SimpleNamespace(fetch_pr=AsyncMock(return_value=pr)), git, DiffParser(), provider,
                            ReportService(), repository=repository, task_queue=queue)
    service._ensure_worker_started = lambda: None
    return service, state, provider, calls, pr


def confirmation(preview):
    return ReviewCreateRequest(pr_url="https://github.com/o/r/pull/1", preview_id=preview.preview_id,
        plan_hash=preview.plan_hash, expected_head_sha=preview.snapshot.head_sha,
        expected_effective_base_sha=preview.snapshot.effective_base_sha)


async def test_preview_confirm_persist_and_replay(plan_service):
    service, state, _, calls, _ = plan_service
    preview = await service.preview(ReviewPreviewRequest(pr_url="https://github.com/o/r/pull/1", model="fixture",
        plan=ReviewPlanOptions(depth="quick", limits=ReviewLimits(max_model_calls=2))))
    task = await service.create_confirmed_task(confirmation(preview))
    assert task.confirmed_plan.depth == "quick" and task.task_runtime_configuration["limits"]["max_calls"] == 2
    assert service.get_task(task.id).plan_hash == preview.plan_hash
    assert (await service.create_confirmed_task(confirmation(preview))).id == task.id
    assert calls == []
    state["task_runtime_configuration"] = task.task_runtime_configuration
    from app.tools.repo_indexer import RepoIndexer
    state.update(await RepoIndexer().execute(repo_path=state["repo_path"],
        call_resolution_version=task.task_runtime_configuration["call_resolution_version"]))
    state["model"] = "fixture"
    planned = await review_plan_node(state)
    assert planned["review_plan"]["review_units"]


async def test_preview_claim_rolls_back_when_enqueue_fails(plan_service, monkeypatch):
    from app.services.task_queue import DatabaseTaskQueue
    service, _, _, _, _ = plan_service
    preview = await service.preview(ReviewPreviewRequest(pr_url="https://github.com/o/r/pull/1"))
    def fail(*args, **kwargs):
        raise RuntimeError("queue_write_failed")
    monkeypatch.setattr(DatabaseTaskQueue, "enqueue", fail)
    with pytest.raises(RuntimeError, match="queue_write_failed"):
        await service.create_confirmed_task(confirmation(preview))
    assert service._repository.get_preview(preview.preview_id)["consumed_task_id"] is None
    assert service.list_tasks(status=None, page=1, page_size=20).total == 0


async def test_confirmation_connection_failure_preserves_plan_and_retry_is_idempotent(plan_service, monkeypatch):
    from sqlalchemy import func, select
    from app.models.orm import WorkerJobOrm
    from app.tools.github_tool import GitHubTool
    service, _, _, model_calls, pr = plan_service
    pr.body = None
    preview = await service.preview(ReviewPreviewRequest(pr_url="https://github.com/o/r/pull/1"))
    request = confirmation(preview)
    failing = True
    requests = []
    def transport(value):
        requests.append(value)
        if failing:
            raise httpx.ConnectError("proxy TLS failure; token=secret", request=value)
        return httpx.Response(200, json={"title": pr.title, "body": pr.body, "html_url": pr.html_url,
            "base": {"sha": pr.base.sha, "ref": pr.base.ref, "repo": {"clone_url": pr.clone_url}},
            "head": {"sha": pr.head.sha, "ref": pr.head.ref, "repo": {"clone_url": pr.head.repo_clone_url}}})
    original = AsyncClient
    monkeypatch.setattr("app.tools.github_tool.httpx.AsyncClient", lambda **kwargs:
        original(transport=httpx.MockTransport(transport), **kwargs))
    service._github_tool = GitHubTool("secret")
    monkeypatch.setattr(reviews, "get_review_service", lambda: service)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/reviews", json=request.model_dump(mode="json"))
        assert response.status_code == 503 and response.json()["detail"]["code"] == "github_connection_failed"
        assert "secret" not in response.text and len(requests) == 3
        assert service._repository.get_preview(preview.preview_id)["consumed_task_id"] is None
        assert service.list_tasks(status=None, page=1, page_size=20).total == 0
        with service._repository._session_factory() as session:
            assert session.scalar(select(func.count()).select_from(WorkerJobOrm)) == 0
        failing = False
        recovered = await client.post("/api/reviews", json=request.model_dump(mode="json"))
        assert recovered.status_code == 202, recovered.text
        replay = await client.post("/api/reviews", json=request.model_dump(mode="json"))
        assert replay.status_code == 202 and replay.json()["task_id"] == recovered.json()["task_id"]
        assert len(requests) == 4
    assert service.list_tasks(status=None, page=1, page_size=20).total == 1 and model_calls == []


def test_externalized_history_title_filter_precedes_pagination(plan_service, monkeypatch):
    service, _, _, _, pr = plan_service
    monkeypatch.setattr(settings, "repoguardian_artifact_inline_max_bytes", 1024)
    for index, title in enumerate(["标题 needle", "无匹配", "标题 needle 第二页"]):
        task = ReviewTask(id=f"history-{index}", pr_url="https://github.com/o/r/pull/1", pr=pr.model_copy(update={"title": title}),
                          report_markdown="audit" * 2000)
        service._repository.create_task(task)
    page = service.list_tasks(status=None, page=2, page_size=1, q="needle")
    assert page.total == 2 and len(page.items) == 1
    assert "needle" in page.items[0].pr.title


@pytest.mark.parametrize("change", ["head", "base", "policy", "expiry", "hash"])
async def test_stale_preview_rejected(plan_service, change):
    service, _, _, _, pr = plan_service
    preview = await service.preview(ReviewPreviewRequest(pr_url="https://github.com/o/r/pull/1"))
    request = confirmation(preview)
    if change == "head":
        pr.head.sha = "f" * 40
    elif change == "base":
        pr.base.sha = "e" * 40
    elif change == "policy":
        service._github_tool.fetch_pr.return_value = pr.model_copy(update={"title": "updated intent"})
    elif change == "hash":
        request.plan_hash = "0" * 64
    else:
        from app.models.orm import ReviewPreviewOrm
        with service._repository._session_factory.begin() as session:
            row = session.get(ReviewPreviewOrm, preview.preview_id)
            payload = copy.deepcopy(row.payload)
            payload["response"]["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            row.payload = payload
    with pytest.raises(ValueError):
        await service.create_confirmed_task(request)
    assert service.list_tasks(status=None, page=1, page_size=20).total == 0


async def test_scope_rejects_unlisted_and_sensitive_file(plan_service):
    service, _, _, _, _ = plan_service
    for path in ["missing.py", ".env"]:
        with pytest.raises(ValueError, match="eligible"):
            await service.preview(ReviewPreviewRequest(pr_url="https://github.com/o/r/pull/1",
                plan=ReviewPlanOptions(scope=ReviewScope(mode="selected", primary_files=[path]))))


async def test_zero_budget_stops_dispatch_and_retains_unchecked(production_input):
    state, provider, calls = production_input
    state["_provider"] = provider
    state["_git_tool"] = GitTool()
    budget = ReviewRunBudget("run", max_calls=0, max_tokens=0)
    with activate_run_budget(budget):
        result = await task_review_runtime_node(state)
    report = result["task_runtime_report"]
    assert calls == [] and report["termination_reason"] == "budget_exhausted"
    assert report["scope_complete"] is False


async def test_focus_and_depth_reach_actual_model_request(production_input):
    state, provider, calls = production_input
    state["_provider"] = provider
    state["_git_tool"] = GitTool()
    state["task_runtime_configuration"]["effective_plan"] = {"depth": "deep", "focus": ["security", "compatibility"]}
    result = await task_review_runtime_node(state)
    assert calls[0]["review_strategy"] == {"depth": "deep", "focus": ["security", "compatibility"]}
    assert "counter-evidence" in calls[0]["review_instructions"]["depth"]
    assert result["task_runtime_report"]["initial_scope_coverage"]


async def test_findings_disposition_history_and_diff_api(plan_service, monkeypatch):
    service, state, _, _, _ = plan_service
    task = ReviewTask(id="task", pr_url="https://github.com/o/r/pull/1", engine="task_review_v1",
        status=TaskStatus.completed_with_warnings, changed_files=state["changed_files"],
        task_runtime_report={"findings": [{"key": "f1", "status": "unresolved", "candidate": {"claim": "candidate"},
            "reason": "insufficient_evidence"}]})
    service._repository.create_task(task)
    monkeypatch.setattr(reviews, "get_review_service", lambda: service)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/system/review-capabilities")
        assert response.status_code == 200 and len(response.json()["depth_presets"]) == 3
        response = await client.patch("/api/reviews/task/findings/f1/disposition", json={"status": "confirmed", "reason": "人工核对"})
        item = response.json()["items"][0]
        assert item["disposition"]["status"] == "confirmed" and item["evidence_status"] == "insufficient"
        assert item["dynamic_validation_status"] == "not_executed"
        assert (await client.patch("/api/reviews/task/findings/missing/disposition", json={"status": "ignored", "reason": "why"})).status_code == 404
        assert (await client.patch("/api/reviews/task/findings/f1/disposition", json={"status": "ignored", "reason": ""})).status_code == 422
        assert (await client.get("/api/reviews?q=absent")).json()["total"] == 0
        assert (await client.get("/api/reviews?q=o%2Fr")).json()["total"] == 1
        assert (await client.get("/api/reviews/task/files/0/diff?limit=1")).status_code == 200
        assert (await client.get("/api/reviews/task/files/0/diff?limit=0")).status_code == 422
        assert (await client.get("/api/reviews/task/files/999/diff")).status_code == 404
    assert service.get_task(task.id).task_runtime_report == task.task_runtime_report


def test_migration_upgrade_and_downgrade(tmp_path):
    from importlib.util import spec_from_file_location, module_from_spec
    from pathlib import Path
    spec = spec_from_file_location("review_plans_migration", Path(__file__).parents[1] / "alembic/versions/20261016_0005_review_plans.py")
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert "review_previews" in inspect(connection).get_table_names()
            migration.downgrade()
            assert "review_previews" not in inspect(connection).get_table_names()
    engine.dispose()
