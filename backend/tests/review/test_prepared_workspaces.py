import asyncio
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from langgraph.runtime import Runtime
from sqlalchemy import select

from app.core.config import Settings, settings
from app.graph.nodes.repo_prepare import repo_prepare_node
from app.models.orm import PreparedWorkspaceOrm, utcnow
from app.models.review import PullRequestInfo, ReviewPreviewRequest
from app.services.workspaces.prepared_workspaces import PreparedWorkspaces, prepare_repository
from app.services.preview.preview_input_cache import input_cache_key
from app.tools.git_tool import GitTool, GitToolCancelled
from app.tools.workspace_cleanup import cleanup_workspace, reap_orphaned_workspaces
from tests.support.persistence import persistence as persistence
from tests.review.test_production_diff_base import _forked_repo, _git
from tests.support.review_plans import confirmation, plan_service as plan_service
from tests.support.runtime_harness import production_input as production_input


@pytest.fixture
def prepared(tmp_path, persistence):
    repository, _, sessions = persistence
    source, root, base, head = _forked_repo(tmp_path)
    pr = PullRequestInfo(owner="local", repo="sample", number=1, title="Fixture",
        html_url="https://github.com/local/sample/pull/1", clone_url=str(source),
        base={"sha": base, "ref": "main", "repo_clone_url": str(source)},
        head={"sha": head, "ref": "feature", "repo_clone_url": str(source)})
    tool = GitTool(tmp_path / "workspaces", command_timeout_seconds=10)
    store = PreparedWorkspaces(repository, tool._workdir)
    configuration = {"call_resolution_version": "import-evidence-v5"}
    path, diff = tool.clone_and_diff(pr)
    context = tool.review_diff_context(path, base, head)
    assert context["effective_base_sha"] == root
    assert store.publish(input_cache_key(pr, configuration), path, pr, context)
    with sessions() as session:
        row = session.scalar(select(PreparedWorkspaceOrm))
        cached_path = store.root / row.directory
    return store, tool, pr, configuration, cached_path, diff, sessions


def test_restarted_store_copies_fork_snapshot_without_network_and_survives_source_removal(prepared, monkeypatch):
    store, tool, pr, configuration, source, diff, _ = prepared
    remote = Mock(side_effect=AssertionError("must not access remote"))
    monkeypatch.setattr(tool, "clone_and_diff", remote)
    commands = []
    original = tool._run

    def record(command, **kwargs):
        commands.append(command)
        return original(command, **kwargs)

    monkeypatch.setattr(tool, "_run", record)
    restarted = PreparedWorkspaces(SimpleNamespace(_session_factory=store.sessions), store.root)
    first, actual = restarted.copy(pr, configuration, tool)
    second, _ = prepare_repository(pr, tool, SimpleNamespace(_session_factory=store.sessions), configuration)
    assert first != second and first != source
    assert actual == diff and _git(first, "rev-parse", "HEAD") == pr.head.sha
    assert not (first / ".git/objects/info/alternates").exists()
    assert not any("fetch" in command for command in commands)
    assert any("--local" in command and "--no-hardlinks" in command for command in commands)
    assert cleanup_workspace(source, workdir=store.root)
    assert _git(first, "show", pr.base.sha + ":base_only.py") == "base_only = True"
    assert _git(first, "show", pr.head.sha + ":x.py")
    (first / "x.py").write_text("changed\n", encoding="utf-8")
    assert "changed" not in (second / "x.py").read_text(encoding="utf-8")
    assert remote.call_count == 0


@pytest.mark.parametrize("damage", ["expired", "missing", "dirty", "head", "base_identity",
                                   "effective_base", "objects", "alternates", "version"])
def test_unusable_workspace_falls_back_with_exact_frozen_pr(prepared, monkeypatch, damage):
    store, tool, pr, configuration, source, _, sessions = prepared
    with sessions.begin() as session:
        row = session.scalar(select(PreparedWorkspaceOrm))
        if damage == "expired":
            row.expires_at = utcnow() - timedelta(seconds=1)
        elif damage == "base_identity":
            row.base_sha = "e" * 40
        elif damage == "effective_base":
            row.effective_base_sha = pr.base.sha
    if damage == "missing":
        cleanup_workspace(source, workdir=store.root)
    elif damage == "dirty":
        (source / "untracked.txt").write_text("dirty", encoding="utf-8")
    elif damage == "head":
        _git(source, "checkout", "--detach", pr.base.sha)
    elif damage == "objects":
        for path in (source / ".git/objects").glob("*/*"):
            if path.is_file():
                path.chmod(0o600)
                path.unlink()
    elif damage == "alternates":
        (source / ".git/objects/info/alternates").write_text(str(source), encoding="utf-8")
    elif damage == "version":
        configuration = {**configuration, "config_version": "changed"}
    remote = Mock(return_value=("remote-copy", "remote-diff"))
    monkeypatch.setattr(tool, "clone_and_diff", remote)
    assert prepare_repository(pr, tool, SimpleNamespace(_session_factory=store.sessions), configuration) == (
        "remote-copy", "remote-diff")
    assert remote.call_args.args == (pr,)
    assert remote.call_args.args[0].head.sha == pr.head.sha
    assert remote.call_args.args[0].base.sha == pr.base.sha
    assert not any(path.name.startswith("local-") for path in store.root.iterdir())


def test_capacity_expiry_and_crash_orphans_have_separate_reclamation(prepared, monkeypatch):
    store, tool, pr, configuration, first, _, sessions = prepared
    monkeypatch.setattr(settings, "repoguardian_prepared_workspace_max_entries", 1)
    second, _ = tool.clone_and_diff(pr)
    assert store.publish("different-key", second, pr,
                         tool.review_diff_context(second, pr.base.sha, pr.head.sha))
    assert not first.exists()
    with sessions.begin() as session:
        row = session.scalar(select(PreparedWorkspaceOrm))
        remaining = store.root / row.directory
        row.expires_at = utcnow() - timedelta(seconds=1)
    orphan = store.root / (".prepared-" + uuid4().hex)
    recent = store.root / (".prepared-" + uuid4().hex)
    orphan.mkdir()
    recent.mkdir()
    os.utime(orphan, (100, 100))
    os.utime(remaining, (100, 100))
    # Even an expired registered source belongs to the dedicated collector.
    generic = reap_orphaned_workspaces(workdir=store.root, older_than_seconds=1)
    assert generic.removed == 0 and remaining.exists()
    assert store.maintain() == 2
    assert not remaining.exists() and not orphan.exists() and recent.exists()
    with sessions() as session:
        assert session.scalar(select(PreparedWorkspaceOrm)) is None


def test_byte_limit_and_disabling_retention_leave_caller_workspace_intact(prepared, monkeypatch):
    store, tool, pr, _, source, _, sessions = prepared
    path, _ = tool.clone_and_diff(pr)
    monkeypatch.setattr(settings, "repoguardian_prepared_workspace_max_bytes", 1)
    assert not store.publish("oversize", path, pr, {})
    assert path.exists() and not source.exists()
    assert not any(row for row in store._rows())
    monkeypatch.setattr(settings, "repoguardian_prepared_workspace_max_entries", 0)
    assert not store.publish("disabled", path, pr, {})
    assert path.exists()


def test_touch_extends_only_live_entries_and_failed_removal_retries(prepared, monkeypatch):
    store, _, pr, configuration, source, _, sessions = prepared
    with sessions.begin() as session:
        row = session.scalar(select(PreparedWorkspaceOrm))
        row.expires_at = utcnow() + timedelta(seconds=1)
    store.touch(input_cache_key(pr, configuration))
    with sessions.begin() as session:
        row = session.scalar(select(PreparedWorkspaceOrm))
        assert row.expires_at > (utcnow() + timedelta(minutes=10)).replace(tzinfo=None)
        row.expires_at = utcnow() - timedelta(seconds=1)
    store.touch(input_cache_key(pr, configuration))
    with monkeypatch.context() as patch:
        patch.setattr("app.services.workspaces.prepared_workspaces.cleanup_workspace", lambda *a, **k: False)
        assert store.maintain() == 0
    with sessions() as session:
        assert session.scalar(select(PreparedWorkspaceOrm)).state == "deleting"
    assert source.exists() and store.maintain() == 1
    assert not source.exists()


def test_corrupt_registered_path_never_reads_or_deletes_outside_workdir(prepared, tmp_path, monkeypatch):
    store, tool, pr, configuration, _, _, sessions = prepared
    outside = tmp_path / "keep.txt"
    outside.write_text("keep", encoding="utf-8")
    with sessions.begin() as session:
        session.scalar(select(PreparedWorkspaceOrm)).directory = "../keep.txt"
    remote = Mock(return_value=("fallback", "diff"))
    monkeypatch.setattr(tool, "clone_and_diff", remote)
    prepare_repository(pr, tool, SimpleNamespace(_session_factory=store.sessions), configuration)
    assert remote.call_count == 1 and outside.read_text(encoding="utf-8") == "keep"
    with sessions() as session:
        assert session.scalar(select(PreparedWorkspaceOrm)) is None


def test_symlinked_source_never_copies_or_deletes_its_target(prepared, tmp_path, monkeypatch):
    store, tool, pr, configuration, source, _, _ = prepared
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    assert cleanup_workspace(source, workdir=store.root)
    try:
        source.symlink_to(outside, target_is_directory=True)
    except OSError:
        if os.name != "nt" or not hasattr(source, "is_junction"):
            pytest.skip("directory links are unavailable")
        linked = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(source), str(outside)],
                                capture_output=True)
        if linked.returncode:
            pytest.skip("directory links are unavailable")
    remote = Mock(return_value=("fallback", "diff"))
    monkeypatch.setattr(tool, "clone_and_diff", remote)
    prepare_repository(pr, tool, SimpleNamespace(_session_factory=store.sessions), configuration)
    assert remote.call_count == 1
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_copy_and_maintenance_are_mutually_exclusive_and_cancel_does_not_fetch(prepared, monkeypatch):
    store, tool, pr, configuration, source, _, sessions = prepared
    entered, release = threading.Event(), threading.Event()
    original = tool.clone_prepared

    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(tool, "clone_prepared", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(store.copy, pr, configuration, tool)
        try:
            assert entered.wait(5)
            with sessions.begin() as session:
                session.scalar(select(PreparedWorkspaceOrm)).expires_at = utcnow() - timedelta(seconds=1)
            assert store.maintain() == 0 and source.exists()
        finally:
            release.set()
        assert pending.result()[0].is_dir()
    cancelled = threading.Event()
    cancelled.set()
    remote = Mock()
    monkeypatch.setattr(tool, "clone_and_diff", remote)
    with pytest.raises(GitToolCancelled):
        prepare_repository(pr, tool, SimpleNamespace(_session_factory=store.sessions),
                           configuration, cancel_event=cancelled)
    assert remote.call_count == 0


def test_cancel_during_checkout_removes_only_partial_copy_and_never_falls_back(prepared, monkeypatch):
    store, tool, pr, configuration, source, _, _ = prepared
    remote = Mock()
    monkeypatch.setattr(tool, "clone_and_diff", remote)
    cancelled = threading.Event()

    def cancel_checkout(event):
        if event.phase == "checkout":
            cancelled.set()

    with pytest.raises(GitToolCancelled):
        prepare_repository(pr, tool, SimpleNamespace(_session_factory=store.sessions),
                           configuration, progress_callback=cancel_checkout, cancel_event=cancelled)
    assert remote.call_count == 0 and source.exists()
    assert not any(path.name.startswith("local-") for path in store.root.iterdir())
    assert len(store._rows()) == 1


def test_failed_registry_write_cleans_moved_directory_without_touching_existing_cache(prepared, monkeypatch):
    from sqlalchemy.orm import Session

    store, tool, pr, _, source, _, _ = prepared
    candidate, _ = tool.clone_and_diff(pr)
    original_add = Session.add

    def fail_add(session, instance, *args, **kwargs):
        if isinstance(instance, PreparedWorkspaceOrm):
            raise RuntimeError("simulated_registry_failure")
        return original_add(session, instance, *args, **kwargs)

    monkeypatch.setattr(Session, "add", fail_add)
    with pytest.raises(RuntimeError, match="simulated_registry_failure"):
        store.publish("new-key", candidate, pr, {})
    assert source.exists() and not candidate.exists()
    assert len(list(store.root.glob(".prepared-*"))) == 2
    assert len(store._rows()) == 1


def test_cross_process_occupancy_is_released_after_process_exit(prepared):
    store, _, _, _, _, _, _ = prepared
    script = """
import os, sys
from types import SimpleNamespace
from app.services.workspaces.prepared_workspaces import PreparedWorkspaces
store = PreparedWorkspaces(SimpleNamespace(_session_factory=None), sys.argv[1])
with store.guard():
    print("locked", flush=True)
    sys.stdin.readline()
    os._exit(0)
"""
    process = subprocess.Popen([sys.executable, "-c", script, str(store.root)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "locked"
        with store.guard(wait=False) as acquired:
            assert acquired is False
    finally:
        process.communicate(input="exit\n", timeout=10)
    with store.guard(wait=False) as acquired:
        assert acquired is True


@pytest.mark.parametrize("field,value", [
    ("repoguardian_prepared_workspace_ttl_seconds", 899),
    ("repoguardian_prepared_workspace_max_entries", -1),
    ("repoguardian_prepared_workspace_max_bytes", -1),
])
def test_retention_configuration_rejects_invalid_limits(field, value):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


async def test_preview_confirmation_and_formal_node_use_one_remote_preparation(plan_service, monkeypatch):
    service, state, _, _, original_pr = plan_service
    # Reuse the actual local Git fixture, replacing only GitHub access.
    source = Path(state["repo_path"])
    tool = GitTool(source.parent / "prepared-workspaces", command_timeout_seconds=10)
    pr = original_pr.model_copy(update={"clone_url": str(source),
        "base": original_pr.base.model_copy(update={"repo_clone_url": str(source)}),
        "head": original_pr.head.model_copy(update={"repo_clone_url": str(source)})})
    service._git_tool = tool
    service._github_tool.fetch_pr = AsyncMock(return_value=pr)
    remote = Mock(wraps=tool.clone_and_diff)
    monkeypatch.setattr(tool, "clone_and_diff", remote)
    preview = await service.preview(ReviewPreviewRequest(pr_url=pr.html_url))
    task = await service.create_confirmed_task(confirmation(preview))
    assert remote.call_count == 1
    frozen_state = {**state, "pr_url": pr.html_url,
                    "_github_tool": service._github_tool,
                    "task_runtime_configuration": task.task_runtime_configuration}
    monkeypatch.setattr("app.graph.nodes.repo_prepare.get_stream_writer", lambda: lambda _: None)
    runtime = Runtime(context={"coordination_repository": service._repository, "task_runtime_git": tool})
    first = await repo_prepare_node(frozen_state, runtime)
    second = await repo_prepare_node(frozen_state, runtime)
    assert first["repo_path"] != second["repo_path"]
    assert first["diff_text"] == second["diff_text"]
    assert remote.call_count == 1 and service._github_tool.fetch_pr.await_count == 4
    from app.graph.nodes.diff_parse import diff_parse_node
    from app.graph.nodes.repo_index import repo_index_node
    from app.tools.repo_indexer import RepoIndexer

    parsed = await diff_parse_node({**frozen_state, **first, "_git_tool": tool})
    assert parsed["changed_files"]
    with monkeypatch.context() as patch:
        patch.setattr(RepoIndexer, "execute", AsyncMock(side_effect=AssertionError("index should be reused")))
        indexed = await repo_index_node({**frozen_state, **first, **parsed}, runtime)
    assert "已复用索引" in indexed["step_progress"][-1]["message"]
    service._github_tool.fetch_pr.return_value = pr.model_copy(update={
        "head": pr.head.model_copy(update={"sha": "f" * 40})})
    with pytest.raises(ValueError, match="PR changed while queued"):
        await repo_prepare_node(frozen_state, runtime)
    assert remote.call_count == 1
    assert await asyncio.to_thread(cleanup_workspace, first["repo_path"], workdir=tool._workdir)


async def test_service_recovery_copies_prepared_source_and_cleans_only_task_copy(prepared, monkeypatch):
    from app.models.review import ReviewTask
    from app.services.review.report_service import ReportService
    from app.services.review.review_service import ReviewService
    from app.tools.diff_parser import DiffParser

    store, tool, pr, configuration, source, _, _ = prepared
    monkeypatch.setattr(settings, "repoguardian_workdir", tool._workdir)
    from app.services.review.review_repository import ReviewRepository
    from app.services.review.artifact_store import LocalArtifactStore

    repository = ReviewRepository(store.sessions, LocalArtifactStore(store.root / "artifacts"),
                                  require_migration=False)
    task = ReviewTask(id="recover-local", pr_url=pr.html_url, engine="task_review_v1",
                      task_runtime_configuration=configuration)
    repository.create_task(task)
    frozen = {"engine": task.engine, "review_plan": {"review_units": []},
              "pr_info": pr.model_dump(mode="json"), "repo_path": str(store.root / "missing"),
              "effective_base_sha": tool.merge_base(source, pr.base.sha, pr.head.sha)}
    compiled = SimpleNamespace(aget_state=AsyncMock(return_value=SimpleNamespace(
        values=frozen, next=("task_review_runtime",))), aupdate_state=AsyncMock())
    monkeypatch.setattr("app.services.review.review_service.get_checkpointer", AsyncMock(return_value=object()))
    monkeypatch.setattr("app.services.review.review_service.build_review_graph",
                        lambda: SimpleNamespace(compile=lambda **_: compiled))
    monkeypatch.setattr(tool, "clone_and_diff", Mock(side_effect=AssertionError("remote recovery")))
    service = ReviewService(None, tool, DiffParser(), None, ReportService(), repository=repository)
    monkeypatch.setattr(service, "_invoke_graph_with_progress",
                        AsyncMock(side_effect=RuntimeError("stop before AI")))
    with pytest.raises(RuntimeError, match="stop before AI"):
        await service._run_graph(task.id)
    copied = Path(compiled.aupdate_state.await_args.args[1]["repo_path"])
    assert copied != source and not copied.exists() and source.exists()
    assert tool.clone_and_diff.call_count == 0


def test_migration_preserves_existing_preview_and_downgrades_only_new_registry(tmp_path):
    from test_artifact_schema_migration import migrate
    from sqlalchemy import create_engine, inspect, text

    database = tmp_path / "migration.db"
    migrate(database, "20261018_0007")
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    try:
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO review_previews (id, payload, expires_at) "
                                    "VALUES ('saved', '{}', '2026-12-01')"))
        migrate(database, "head")
        assert "prepared_workspaces" in inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert connection.execute(text("SELECT id FROM review_previews")).scalar_one() == "saved"
        migrate(database, "20261018_0007", direction="downgrade")
        assert "prepared_workspaces" not in inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert connection.execute(text("SELECT id FROM review_previews")).scalar_one() == "saved"
    finally:
        engine.dispose()
