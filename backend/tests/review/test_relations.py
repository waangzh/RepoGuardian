import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from app.languages import isolation
from app.models.review.diff import ChangedFile
from app.review.domain import Options, Snapshot
from app.review.planner import plan_review
from app.review.relations import change_relations
import app.review.relations as relations_module


def snapshot():
    return Snapshot(review_id="run", repository="o/r", repository_id=1, pr_number=1,
                    declared_base_sha="a" * 40, base_sha="a" * 40, head_sha="b" * 40)


def change(path, **kwargs):
    return ChangedFile(file_path=path, change_type=kwargs.pop("change_type", "modified"),
                       additions=kwargs.pop("additions", 1), deletions=kwargs.pop("deletions", 0),
                       **kwargs)


def write(repo, path, content):
    target = Path(repo) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def test_resolved_import_groups_producer_and_consumer(tmp_path):
    write(tmp_path, "app/services/order.py", "def create():\n    return 1\n")
    write(tmp_path, "app/api/routes.py",
          "from app.services.order import create\n\n\ndef handler():\n    return create()\n")
    changes = [change("app/services/order.py"), change("app/api/routes.py")]
    relations = change_relations(tmp_path, changes, Options())
    assert (0, "app/api/routes.py", "app/services/order.py") in relations
    plan = plan_review(changes, snapshot(), Options(), relations)
    assert any({"app/api/routes.py", "app/services/order.py"} <= set(unit.paths)
               for unit in plan.units)


def test_relation_edge_survives_capacity_packing(tmp_path):
    write(tmp_path, "a/order.py", "def create():\n    return 1\n")
    write(tmp_path, "z/routes.py", "from a.order import create\n")
    for path in ("m/one.py", "n/two.py", "o/three.py"):
        write(tmp_path, path, "value = 1\n")
    changes = [change(path) for path in
               ("a/order.py", "m/one.py", "n/two.py", "o/three.py", "z/routes.py")]
    relations = change_relations(tmp_path, changes, Options())
    plan = plan_review(changes, snapshot(), Options(), relations)
    assert any({"a/order.py", "z/routes.py"} <= set(unit.paths) for unit in plan.units)


def test_imported_name_resolves_submodule(tmp_path):
    write(tmp_path, "app/services/order.py", "def create():\n    return 1\n")
    write(tmp_path, "app/api/routes.py", "from app.services import order\n")
    changes = [change("app/services/order.py"), change("app/api/routes.py")]
    assert (0, "app/api/routes.py", "app/services/order.py") in change_relations(
        tmp_path, changes, Options())


def test_stem_fallback_import_does_not_group(tmp_path):
    write(tmp_path, "app/services/order.py", "def create():\n    return 1\n")
    write(tmp_path, "app/api/routes.py", "from order import create\n")
    changes = [change("app/services/order.py"), change("app/api/routes.py")]
    assert not change_relations(tmp_path, changes, Options())


def test_strong_test_pair_groups_but_weak_pair_does_not(tmp_path):
    write(tmp_path, "app/services/order.py", "def create():\n    return 1\n")
    write(tmp_path, "tests/test_order.py", "def test_create():\n    assert True\n")
    changes = [change("app/services/order.py"), change("tests/test_order.py")]
    assert (1, "app/services/order.py", "tests/test_order.py") in change_relations(
        tmp_path, changes, Options())
    write(tmp_path, "tests/test_misc.py", "def test_misc():\n    assert True\n")
    weak = [change("app/services/order.py"), change("tests/test_misc.py")]
    assert not change_relations(tmp_path, weak, Options())


def test_same_directory_only_pairs_two_files(tmp_path):
    write(tmp_path, "pkg/a.py", "a = 1\n")
    write(tmp_path, "pkg/b.py", "b = 2\n")
    assert (2, "pkg/a.py", "pkg/b.py") in change_relations(
        tmp_path, [change("pkg/a.py"), change("pkg/b.py")], Options())
    write(tmp_path, "pkg/c.py", "c = 3\n")
    assert not change_relations(
        tmp_path, [change("pkg/a.py"), change("pkg/b.py"), change("pkg/c.py")], Options())


def test_sensitive_and_user_excluded_paths_are_not_read(tmp_path, monkeypatch):
    write(tmp_path, ".env", "SECRET=1\n")
    write(tmp_path, "a.py", "a = 1\n")
    write(tmp_path, "b.py", "b = 2\n")
    calls = []
    real = relations_module.validate_repository_file

    def spy(root, path, **kwargs):
        calls.append(path)
        return real(root, path, **kwargs)

    monkeypatch.setattr(relations_module, "validate_repository_file", spy)
    changes = [change(".env"), change("a.py"), change("b.py")]
    relations = change_relations(tmp_path, changes, Options(paths=["a.py"]))
    assert calls == ["a.py"]
    assert relations == []


def test_parser_failure_degrades_without_raising(tmp_path, monkeypatch):
    write(tmp_path, "pkg/a.py", "a = 1\n")
    write(tmp_path, "pkg/b.py", "b = 2\n")

    def boom(*args, **kwargs):
        raise RuntimeError("parser failed")

    monkeypatch.setattr(relations_module, "analyze_isolated", boom)
    relations = change_relations(
        tmp_path, [change("pkg/a.py"), change("pkg/b.py")], Options())
    assert relations == [(2, "pkg/a.py", "pkg/b.py")]


def test_rename_old_path_aliases_consumer_edge(tmp_path):
    write(tmp_path, "app/services/order.py", "def create():\n    return 1\n")
    write(tmp_path, "app/api/routes.py",
          "from app.services.legacy_order import create\n\n\ndef handler():\n    return create()\n")
    changes = [change("app/services/order.py", old_file_path="app/services/legacy_order.py",
                      change_type="renamed", additions=0, deletions=0),
               change("app/api/routes.py")]
    assert (0, "app/api/routes.py", "app/services/order.py") in change_relations(
        tmp_path, changes, Options())


def test_relations_are_deterministic_under_input_order(tmp_path):
    write(tmp_path, "app/services/order.py", "def create():\n    return 1\n")
    write(tmp_path, "app/api/routes.py", "from app.services.order import create\n")
    write(tmp_path, "tests/test_order.py", "def test_create():\n    assert True\n")
    changes = [change("app/services/order.py"), change("app/api/routes.py"),
               change("tests/test_order.py")]
    first = change_relations(tmp_path, changes, Options())
    second = change_relations(tmp_path, list(reversed(changes)), Options())
    assert first == second
    assert plan_review(changes, snapshot(), Options(), first) == plan_review(
        list(reversed(changes)), snapshot(), Options(), second)


def forbid_source_reads(monkeypatch, path):
    original = Path.read_bytes

    def read(candidate):
        assert candidate != path, "refused source must not be read"
        return original(candidate)

    monkeypatch.setattr(Path, "read_bytes", read)


def test_git_symlink_mode_is_rejected_without_reading_target(tmp_path, monkeypatch):
    write(tmp_path, ".env", "PRIVATE = 1\n")
    write(tmp_path, "alias.py", ".env")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    blob = subprocess.run(
        ["git", "-C", str(tmp_path), "hash-object", "-w", "--stdin"],
        input=b".env", check=True, capture_output=True).stdout.decode().strip()
    # A real index entry exercises the mode gate even when Windows cannot create links.
    subprocess.run(["git", "-C", str(tmp_path), "update-index", "--add", "--cacheinfo",
                    f"120000,{blob},alias.py"], check=True, capture_output=True)
    forbid_source_reads(monkeypatch, tmp_path / "alias.py")
    assert change_relations(tmp_path, [change("alias.py")], Options()) == []


def test_empty_git_index_does_not_allow_untracked_source(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    write(tmp_path, "untracked.py", "VALUE = 1\n")
    forbid_source_reads(monkeypatch, tmp_path / "untracked.py")
    assert change_relations(tmp_path, [change("untracked.py")], Options()) == []


def test_sensitive_symlink_target_is_not_read_in_non_git_fixture(tmp_path, monkeypatch):
    write(tmp_path, ".env", "PRIVATE = 1\n")
    try:
        os.symlink(".env", tmp_path / "alias.py")
    except OSError:
        pytest.skip("symbolic links are unavailable")
    forbid_source_reads(monkeypatch, tmp_path / ".env")
    assert change_relations(tmp_path, [change("alias.py")], Options()) == []


@pytest.fixture
def parser_runtime():
    isolation.close_parser_workers()
    yield
    isolation.close_parser_workers()


@pytest.mark.parametrize("stop", ["deadline", "caller"])
def test_relation_stop_reaps_active_parser_without_quarantine(
    tmp_path, monkeypatch, parser_runtime, stop,
):
    write(tmp_path, "pkg/a.py", "VALUE = 1\n")
    worker = tmp_path / "trusted_test_worker.py"
    worker.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    monkeypatch.setattr(isolation, "_WORKER", worker)
    monkeypatch.setattr(isolation, "PARSER_TIMEOUT_SECONDS", 3.0)
    monkeypatch.setattr(relations_module, "RELATION_DEADLINE_SECONDS",
                        0.8 if stop == "deadline" else 5.0)
    processes, fallbacks = [], []
    original = subprocess.Popen

    def record_process(argv, **kwargs):
        process = original(argv, **kwargs)
        if str(worker) in argv:
            processes.append(process)
        return process

    adapter = relations_module.default_language_registry.for_path("pkg/a.py")
    original_fallback = adapter.analyze_source

    def fallback(*args, **kwargs):
        fallbacks.append(True)
        return original_fallback(*args, **kwargs)

    monkeypatch.setattr(isolation.subprocess, "Popen", record_process)
    monkeypatch.setattr(adapter, "analyze_source", fallback)
    cancel = threading.Event()
    timer = threading.Timer(0.8, cancel.set) if stop == "caller" else None
    if timer:
        timer.start()
    started = time.monotonic()
    try:
        result = change_relations(tmp_path, [change("pkg/a.py")], Options(),
                                  cancel_event=cancel)
    finally:
        if timer:
            timer.cancel()
            timer.join()
    assert time.monotonic() - started < 2.5
    assert result == []
    assert len(processes) == 1 and processes[0].poll() is not None
    assert not fallbacks
    assert not isolation._RECENT_FAILURES
    assert not list(isolation._failure_directory().glob("*.json"))


def test_relation_deadline_cancels_wait_for_parser_slot(
    tmp_path, monkeypatch, parser_runtime,
):
    write(tmp_path, "pkg/a.py", "VALUE = 1\n")
    monkeypatch.setattr(isolation, "_SLOTS", threading.BoundedSemaphore(0))
    monkeypatch.setattr(isolation, "PARSER_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(relations_module, "RELATION_DEADLINE_SECONDS", 0.2)
    assert change_relations(tmp_path, [change("pkg/a.py")], Options()) == []
    assert not isolation._RECENT_FAILURES
    assert not list(isolation._failure_directory().glob("*.json"))
