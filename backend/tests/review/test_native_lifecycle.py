import shutil
import asyncio
import subprocess
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.review.diff import PullRequestInfo
from app.review.contracts import ReviewCreateRequest, ReviewPreviewRequest
from app.services.publication.publication_planner import PublicationPlanner
from app.services.publication.publication_repository import PublicationRepository, PublicationConflict
from app.models.publication import PublicationSelection
from app.core.fingerprints import stable_hash
from app.services.review.artifact_store import LocalArtifactStore
from app.services.review.report_service import ReportService
from app.services.review.review_repository import ReviewLeaseLost, ReviewRepository, ReviewStopped
from app.services.review.review_service import ReviewService
from app.tools.diff_parser import DiffParser
from app.tools.git_tool import GitTool
from tests.review.test_native_graph import ReviewModel

pytest_plugins = ["tests.review.test_native_agents"]


@pytest.fixture
def pipeline(source_service, tmp_path, monkeypatch):
    source = source_service
    workdir = tmp_path / "workspaces"
    workdir.mkdir()

    class LocalGit(GitTool):
        def clone_and_diff(self, pr, **kwargs):
            destination = workdir / f"local-repo-1-{len(list(workdir.iterdir())):08d}"
            shutil.copytree(source.repo_path, destination)
            diff = subprocess.run(
                ["git", "-C", str(destination), "diff", "--no-ext-diff",
                 source.snapshot.base_sha, source.snapshot.head_sha],
                check=True, capture_output=True,
            ).stdout.decode()
            return destination, diff

    pr = PullRequestInfo(owner="local", repo="repo", number=1, title="Change value",
        body="保留固定快照", html_url="https://github.com/local/repo/pull/1",
        clone_url="https://github.com/local/repo.git",
        base={"ref": "main", "sha": source.snapshot.base_sha,
              "repo_clone_url": "https://github.com/local/repo.git"},
        head={"ref": "change", "sha": source.snapshot.head_sha,
              "repo_clone_url": "https://github.com/local/repo.git"})

    class GitHub:
        async def fetch_pr(self, url):
            return pr.model_copy(deep=True)

    model = ReviewModel()
    provider = SimpleNamespace(
        _provider_name="fixture", _base_url="https://example.test/v1", _default_model="fixture",
        agent_model=lambda *args, **kwargs: model)
    engine = create_engine(f"sqlite:///{(tmp_path / 'business.sqlite').as_posix()}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    repository = ReviewRepository(sessionmaker(engine, expire_on_commit=False),
        LocalArtifactStore(tmp_path / "artifacts"), require_migration=False)
    service = ReviewService(GitHub(), LocalGit(workdir), DiffParser(), provider, ReportService(),
                             repository=repository)
    monkeypatch.setattr(service, "start_worker", lambda: None)
    saver = InMemorySaver()

    async def checkpointer():
        return saver

    monkeypatch.setattr("app.services.review.review_service.get_checkpointer", checkpointer)
    # Workspace cache behavior has independent filesystem tests; this fixture
    # deliberately exercises fresh immutable inputs on each preparation.
    monkeypatch.setattr("app.services.workspaces.prepared_workspaces.prepare_repository",
                        lambda pr, git, *args, **kwargs: git.clone_and_diff(pr, **kwargs))
    monkeypatch.setattr("app.services.workspaces.prepared_workspaces.PreparedWorkspaces.publish",
                        lambda *args, **kwargs: False)
    yield service, repository, model, pr, engine
    engine.dispose()


async def preview_and_create(service):
    request = ReviewPreviewRequest(
        pr_url="https://github.com/local/repo/pull/1",
        plan={"scope": {"mode": "selected", "primary_files": ["safe.py"]},
              "include_pr_discussions": False},
    )
    preview = await service.preview(request)
    created = ReviewCreateRequest(pr_url=request.pr_url, preview_id=preview.preview_id,
        expected_head_sha=preview.snapshot.head_sha,
        expected_effective_base_sha=preview.snapshot.effective_base_sha)
    return preview, created, await service.create_confirmed_task(created)


async def test_persistent_pipeline_confirmation_native_graph_report_and_revalidated_drafts(pipeline, monkeypatch):
    from app.services.review import review_service

    build_graph = review_service.build_review_graph
    durability = []

    def synchronous_graph(saver):
        graph = build_graph(saver)
        invoke = graph.ainvoke

        async def checked_invoke(*args, **kwargs):
            durability.append(kwargs.get("durability"))
            return await invoke(*args, **kwargs)

        monkeypatch.setattr(graph, "ainvoke", checked_invoke)
        return graph

    monkeypatch.setattr(review_service, "build_review_graph", synchronous_graph)
    service, repository, model, _, _ = pipeline
    preview, request, task = await preview_and_create(service)
    assert len(preview.review_units) == 1 and len(preview.assignments) == 1
    replay = await service.create_confirmed_task(request)
    assert replay.id == task.id
    assert await service._worker.run_once()
    saved = repository.get_task(task.id)
    assert saved.status == "completed", saved.error
    assert saved.review_report.complete
    assert len(saved.review_report.findings) == 1
    assert saved.report_markdown
    assert len(model.observed) == 2
    assert durability == ["sync"]
    plan = PublicationPlanner(repository._artifacts).build(saved)
    assert plan.content_eligible
    assert plan.outcome.confirmed_count == plan.outcome.publishable_count == 1
    assert len(plan.inline_drafts) == 1
    assert plan.snapshot.head_sha == saved.snapshot.head_sha
    assert not await service._worker.run_once()
    assert len(model.observed) == 2


async def test_planning_limitations_survive_summary_and_report_without_changing_goal_coverage(pipeline):
    from app.review.models import ReviewLimits
    from app.review.planning import plan_review
    from app.services.publication.publication_planner import summary_projection
    from tests.review.test_native_graph import CHANGE

    service, repository, _, _, _ = pipeline
    _, _, task = await preview_and_create(service)
    assert await service._worker.run_once()
    saved = repository.get_task(task.id)
    limited = plan_review([{**CHANGE, "file_path": "auth.py"}],
                          depth="standard", limits=ReviewLimits(total_model_calls=4))
    assert [item.reviewer for item in limited.assignments] == ["reviewer"]
    assert limited.limitations
    saved.review_report.coverage_limitations = limited.limitations
    saved.review_report.findings = []
    saved.review_report.candidates = []
    saved.review_report.decisions = []
    repository.save_task(saved)

    reopened = repository.get_task(task.id)
    plan = PublicationPlanner(repository._artifacts).build(reopened)
    assert reopened.review_report.complete
    assert plan.outcome.coverage == "complete"
    assert plan.outcome.kind == "inconclusive"
    assert "review_plan_limited" in plan.outcome.reason_codes
    assert plan.digest.merge_decision.status == "resolve_unknowns"
    summary = summary_projection(plan)
    for limitation in limited.limitations:
        assert limitation in summary.digest.merge_decision.limits
        assert limitation in plan.user_report_markdown
        assert limitation in plan.review_comment_markdown


async def test_execution_plan_uses_index_without_expanding_confirmed_scope(pipeline, monkeypatch):
    service, repository, _, pr, _ = pipeline
    prepare = service._prepare

    async def related_prepare(*args, **kwargs):
        repo, prepared = await prepare(*args, **kwargs)
        if "index" not in prepared:
            return repo, prepared
        prepared["index"]["symbol_index"].append(
            {"file": "safe.py", "symbol_id": "safe::value", "start_line": 1, "end_line": 1})
        prepared["index"]["repository_graph"]["edges"].extend([{
            "type": "calls", "source_kind": "symbol", "target_kind": "symbol",
            "source": "safe::value", "target": "removed::old",
            "source_file": "safe.py", "target_file": "removed.py",
            "resolution_status": "resolved", "confidence": 0.99}, {
            "type": "imports", "source_kind": "file", "target_kind": "file",
            "source": "safe.py", "target": "other.py", "confidence": 0.99}])
        return repo, prepared

    monkeypatch.setattr(service, "_prepare", related_prepare)
    preview = await service.preview(ReviewPreviewRequest(
        pr_url=pr.html_url, plan={"include_pr_discussions": False,
            "scope": {"mode": "selected", "primary_files": ["safe.py", "removed.py"]}}))
    assert preview.review_units
    assert all(not unit.changed_symbols and not unit.impacted_paths
               for unit in preview.review_units)
    preview_paths = sorted(path for unit in preview.review_units for path in unit.changed_paths)
    assert preview_paths == ["removed.py", "safe.py"]
    task = await service.create_confirmed_task(ReviewCreateRequest(pr_url=pr.html_url,
        preview_id=preview.preview_id, expected_head_sha=preview.snapshot.head_sha,
        expected_effective_base_sha=preview.snapshot.effective_base_sha))
    assert await service._worker.run_once()
    report = repository.get_task(task.id).review_report
    assert report is not None
    # 执行计划使用索引合并关联变更，但不扩大用户确认的文件范围。
    assert sorted(path for unit in report.review_units for path in unit.changed_paths) == preview_paths
    assert len(report.review_units) < len(preview.review_units)
    assert any(unit.changed_symbols == ["safe::value"] for unit in report.review_units)
    assert any("other.py" in unit.impacted_paths for unit in report.review_units)
    assert {item.id for item in report.goals} == {item.id for item in preview.goals}
    assert all(not unit.changed_symbols and not unit.impacted_paths
               for unit in preview.review_units)  # 预览估算与执行补充分开保留


@pytest.mark.parametrize("depth", ["quick", "standard", "deep"])
async def test_preview_skips_index_and_execution_uses_requested_depth(pipeline, monkeypatch, depth):
    from app.tools import repo_indexer

    scopes = []
    original = repo_indexer.RepoIndexer.execute

    async def execute(self, **kwargs):
        paths = kwargs.get("changed_paths")
        scopes.append(None if paths is None else frozenset(paths))
        return await original(self, **kwargs)

    monkeypatch.setattr(repo_indexer.RepoIndexer, "execute", execute)
    service, _, _, _, _ = pipeline
    preview = await service.preview(ReviewPreviewRequest(
        pr_url="https://github.com/local/repo/pull/1",
        plan={"depth": depth, "include_pr_discussions": False,
              "scope": {"mode": "selected", "primary_files": ["safe.py"]}}))
    assert scopes == []
    task = await service.create_confirmed_task(ReviewCreateRequest(
        pr_url="https://github.com/local/repo/pull/1", preview_id=preview.preview_id,
        expected_head_sha=preview.snapshot.head_sha,
        expected_effective_base_sha=preview.snapshot.effective_base_sha))
    assert await service._worker.run_once()
    assert scopes == [None if depth == "deep" else frozenset({"safe.py"})]
    assert service.get_task(task.id).status == "completed"


async def test_symbol_reads_and_relations_expand_shared_index_once(
    pipeline, source_service, monkeypatch,
):
    from dataclasses import replace
    from app.services.review import review_service
    from app.review.tools import find_references, get_callers, read_symbol
    from app.tools.repo_indexer import RepoIndexer

    service, _, _, pr, _ = pipeline
    (source_service.repo_path / "other.py").write_text(
        "def other():\n    return 2\n", encoding="utf-8", newline="\n")
    (source_service.repo_path / "consumer.py").write_text(
        "from other import other\ndef consumer():\n    return other()\n",
        encoding="utf-8", newline="\n")
    for args in (("add", "other.py", "consumer.py"),
                 ("-c", "user.name=Test", "-c", "user.email=test@example.test",
                  "commit", "-m", "add callable fixture")):
        subprocess.run(["git", "-C", str(source_service.repo_path), *args],
                       check=True, capture_output=True)
    head = subprocess.run(["git", "-C", str(source_service.repo_path), "rev-parse", "HEAD"],
                          check=True, capture_output=True).stdout.decode().strip()
    source_service.snapshot.head_sha = pr.head.sha = head
    scopes = []
    observed = {}
    execute = RepoIndexer.execute

    async def indexed(self, **kwargs):
        paths = kwargs.get("changed_paths")
        scopes.append(None if paths is None else frozenset(paths))
        return await execute(self, **kwargs)

    monkeypatch.setattr(RepoIndexer, "execute", indexed)
    build_graph = review_service.build_review_graph

    def graph(saver):
        result = build_graph(saver)
        invoke = result.ainvoke

        async def checked(*args, context, **kwargs):
            sources = await context.sources()
            branch = replace(sources)
            runtime = SimpleNamespace(context=branch)
            assert not any(item["file"] == "other.py" for item in sources.symbols)
            symbol = await read_symbol.coroutine(path="other.py", symbol="other", runtime=runtime)
            observed["symbol"] = symbol
            assert symbol["ok"] and "return 2" in symbol["content"]
            assert any(item["file"] == "other.py" for item in sources.symbols)
            before = len(scopes)
            denied = await read_symbol.coroutine(path=".env", symbol="SECRET", runtime=runtime)
            assert not denied["ok"] and len(scopes) == before
            callers, references = await asyncio.gather(
                get_callers.coroutine(symbol="other", runtime=runtime),
                find_references.coroutine(symbol="other", runtime=runtime))
            observed.update(callers=callers, references=references)
            assert callers["scope_complete"] and references["scope_complete"]
            assert any(edge["source_file"] == "consumer.py" for edge in callers["edges"])
            assert scopes.count(None) == 1
            assert sources.repository_graph["metadata"]["scope_mode"] == "full"
            return await invoke(*args, context=context, **kwargs)

        monkeypatch.setattr(result, "ainvoke", checked)
        return result

    monkeypatch.setattr(review_service, "build_review_graph", graph)
    _, _, task = await preview_and_create(service)
    assert await service._worker.run_once()
    assert service.get_task(task.id).status == "completed", observed
    assert len(scopes) == 3


async def test_cancelled_review_refuses_lazy_index_expansion(pipeline, monkeypatch):
    from app.services.review import review_service
    from app.tools.repo_indexer import RepoIndexer

    service, repository, _, _, _ = pipeline
    calls = []
    workspaces = []
    execute = RepoIndexer.execute

    async def indexed(self, **kwargs):
        calls.append(kwargs.get("changed_paths"))
        return await execute(self, **kwargs)

    monkeypatch.setattr(RepoIndexer, "execute", indexed)
    build_graph = review_service.build_review_graph

    def graph(saver):
        result = build_graph(saver)

        async def cancelled(*args, context, **kwargs):
            sources = await context.sources()
            workspaces.append(sources.evidence.repo_path)
            assert service.cancel_task(sources.evidence.snapshot.review_id)
            await sources.ensure_index(None)
            pytest.fail("cancelled review expanded its index")

        monkeypatch.setattr(result, "ainvoke", cancelled)
        return result

    monkeypatch.setattr(review_service, "build_review_graph", graph)
    _, _, task = await preview_and_create(service)
    with pytest.raises(ReviewStopped):
        await service._run_graph(task.id)
    assert calls == [{"safe.py"}]
    assert repository.get_task(task.id).status == "cancelled"
    assert workspaces and not workspaces[0].exists()


async def test_confirmation_refuses_changed_pr_and_unapproved_plan(pipeline):
    service, _, _, pr, _ = pipeline
    preview = await service.preview(ReviewPreviewRequest(
        pr_url=pr.html_url, plan={"include_pr_discussions": False}))
    pr.head.sha = "0" * 40
    with pytest.raises(ValueError, match="stale"):
        await service.create_confirmed_task(ReviewCreateRequest(
            pr_url=pr.html_url, preview_id=preview.preview_id,
            expected_head_sha=preview.snapshot.head_sha,
            expected_effective_base_sha=preview.snapshot.effective_base_sha))
    with pytest.raises(ValueError):
        ReviewCreateRequest.model_validate({"pr_url": pr.html_url})


async def test_cancelled_task_and_lost_lease_refuse_late_writes(pipeline):
    service, repository, model, _, _ = pipeline
    _, _, task = await preview_and_create(service)
    job = service._task_queue.claim(worker_id="owner")
    assert job
    lease = {"job_id": job.id, "owner": "other", "attempt": job.attempts}
    with pytest.raises(ReviewLeaseLost):
        repository.assert_active(task.id, lease)
    assert service.cancel_task(task.id)
    with pytest.raises(ReviewStopped):
        repository.save_task(task)
    await service._run_graph(task.id, job=job)
    assert repository.get_task(task.id).status == "cancelled"
    assert model.observed == []


async def test_publication_refuses_tampered_verdict_or_missing_evidence_archive(pipeline):
    service, repository, _, _, _ = pipeline
    _, _, task = await preview_and_create(service)
    assert await service._worker.run_once()
    saved = repository.get_task(task.id)
    assert saved.review_report is not None, saved.error
    tampered = saved.model_copy(deep=True)
    tampered.review_report.findings[0].candidate.title = "另一条未经核验的问题"
    plan = PublicationPlanner(repository._artifacts).build(tampered)
    assert not plan.inline_drafts and not plan.summary_findings
    assert plan.withheld_findings
    identity = saved.review_report.findings[0].evidence_ids[0]
    evidence = repository._artifacts.root / saved.id / f"evidence-{identity}.txt"
    evidence.unlink()
    plan = PublicationPlanner(repository._artifacts).build(saved)
    assert not plan.inline_drafts and plan.outcome.publishable_count == 0


async def test_native_investigation_limits_survive_report_projection_without_legacy_digest(pipeline):
    service, repository, _, _, _ = pipeline
    _, _, task = await preview_and_create(service)
    assert await service._worker.run_once()
    saved = repository.get_task(task.id)
    assert saved.review_report.termination == "completed"
    saved.review_report.discovery_results[0].investigation_limitations = ["调用方搜索还有未核实范围"]
    plan = PublicationPlanner(repository._artifacts).build(saved)
    assert plan.outcome.incomplete_investigations == 0 and plan.outcome.pending_limitation_count == 1
    assert "limitation_assessment_incomplete" in plan.outcome.reason_codes
    assert plan.outcome.runtime_reliability == "reliable"
    assert plan.presentation.verdict.status in {"fix_blockers", "resolve_unknowns"}
    label = "仍有审查限制声明尚未完成必要性复核"
    assert any(label in item for item in plan.presentation.limitations)
    for text in (plan.summary_markdown, plan.review_comment_markdown, plan.user_report_markdown):
        assert label in text
        assert "调查状态未知" not in text
    assert plan.digest.findings
    assert plan.outcome.confirmed_count == len(saved.review_report.findings)


async def test_outbox_idempotency_and_cancelled_publication_boundary(pipeline):
    service, review_repo, _, _, _ = pipeline
    _, _, task = await preview_and_create(service)
    assert await service._worker.run_once()
    saved = review_repo.get_task(task.id)
    plan = PublicationPlanner(review_repo._artifacts).build(saved)
    keys = [draft.finding_key for draft in plan.inline_drafts]
    selection = PublicationSelection(expected_plan_hash=stable_hash(plan.model_dump(mode="json")),
        expected_head_sha=plan.snapshot.head_sha, finding_keys=keys)
    context = {"repository_id": 123, "pr_number": 1, "publisher_identity": "user:7",
        "owner": "local", "repo": "repo", "base_sha": saved.snapshot.declared_base_sha,
        "order": saved.completed_at.isoformat() + ":" + saved.id, "publisher_id": 7}
    repo = PublicationRepository(review_repo._session_factory)
    one = repo.create(plan, selection, context, "request-key")
    replay = repo.create(plan, selection, context, "request-key")
    assert one.id == replay.id
    lease = repo.acquire(one, "worker")
    repo.release(lease)
    with review_repo._session_factory.begin() as session:
        from app.models.orm import ReviewTaskOrm
        row = session.get(ReviewTaskOrm, task.id)
        row.status = "cancelled"
    with pytest.raises(PublicationConflict, match="review_not_publishable"):
        repo.create(plan, selection, context, "new-request")
    with pytest.raises(PublicationConflict, match="review_not_publishable"):
        repo.acquire(one, "another-worker")


async def test_cancel_api_thread_stops_running_model_and_blocks_late_completion(pipeline):
    service, repository, model, _, _ = pipeline
    entered = asyncio.Event()
    original = model._agenerate

    async def waiting(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
        return await original(*args, **kwargs)

    object.__setattr__(model, "_agenerate", waiting)
    _, _, task = await preview_and_create(service)
    runner = asyncio.create_task(service._worker.run_once())
    await asyncio.wait_for(entered.wait(), 15)
    assert await asyncio.to_thread(service.cancel_task, task.id)
    await asyncio.wait_for(runner, 10)
    saved = repository.get_task(task.id)
    assert saved.status == "cancelled"
    assert saved.review_report is None
    assert PublicationPlanner(repository._artifacts).build(saved).inline_drafts == []


async def test_completed_graph_result_survives_report_failure_without_new_model_calls(pipeline):
    service, repository, model, _, _ = pipeline
    original = service._report_service.generate
    fail = True

    def report(task):
        nonlocal fail
        if fail:
            fail = False
            raise OSError("injected report write boundary")
        return original(task)

    service._report_service.generate = report
    _, _, task = await preview_and_create(service)
    assert await service._worker.run_once()
    saved = repository.get_task(task.id)
    assert saved.status == "failed"
    calls = len(model.observed)
    resumed = await service.resume_review(task.id)
    assert resumed.status == "queued"
    assert await service._worker.run_once()
    assert repository.get_task(task.id).status == "completed"
    assert len(model.observed) == calls
