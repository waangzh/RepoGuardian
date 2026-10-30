import asyncio
import hashlib
import json
import subprocess
from types import SimpleNamespace
import pytest
from app.review.retrieval import find_paths, search
from tests.support.source_fixtures import commit_sources

def context_for(service):
    return SimpleNamespace(evidence=service, denied_paths=frozenset(), check_active=None, cursors={})



async def test_late_file_and_in_file_hits_are_reachable_at_fixed_snapshot(source_service, monkeypatch):
    texts = {f"pkg/f{index:03d}.py": "unrelated\n" for index in range(130)}
    texts["pkg/f129.py"] = "NEEDLE\n" * 27
    commit_sources(source_service, texts)
    (source_service.repo_path / "pkg/f129.py").write_text("dirty tree\n", encoding="utf-8")
    context = context_for(source_service)
    searches = []
    original = source_service.git.find_matching_paths

    def located(*args):
        searches.append(args[2])
        return original(*args)

    monkeypatch.setattr(source_service.git, "find_matching_paths", located)
    pages, cursor = [], ""
    while True:
        page = await search(context, "NEEDLE", cursor=cursor)
        assert page["ok"] and not page["truncated"] and "evidence" not in page
        assert len(json.dumps(page)) < 24000
        pages.append(page)
        if not page["has_more"]:
            break
        assert len(pages) < 10
        cursor = page["next_cursor"]
    assert pages[0]["matches"][0]["path"] == "pkg/f129.py" and pages[0]["has_more"]
    assert [item["line"] for page in pages for item in page["matches"]] == list(range(1, 28))
    assert all(item["path"] == "pkg/f129.py" for page in pages for item in page["matches"])
    assert searches == ["NEEDLE"]  # Subsequent hit pages reuse the signed bounded search window.
    filtered = await search(context, "NEEDLE", path="pkg", glob="*129.py", limit=40)
    assert len(filtered["matches"]) == 27 and not filtered["has_more"]
    base = await search(context, "OLD", path="removed.py", side="base")
    assert base["matches"][0]["line"] == 1
    head = await search(context, "OLD", path="removed.py")
    assert head["matches"] == [] and head["unreadable_files"] == 0


async def test_path_pagination_query_and_snapshot_binding(source_service):
    source_service.allowed_paths = frozenset(f"pkg/f{i:03d}.py" for i in range(170))
    context = context_for(source_service)
    pages, cursor = [], ""
    while True:
        page = find_paths(context, path="pkg", glob="*.py", cursor=cursor)
        pages.extend(page["paths"])
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
    assert pages == sorted(source_service.allowed_paths)
    first = find_paths(context)
    for kwargs in ({"query": "f"}, {"path": "pkg"}, {"glob": "*.py"}):
        with pytest.raises(ValueError, match="invalid_cursor"):
            find_paths(context, cursor=first["next_cursor"], **kwargs)
    source_service.snapshot = source_service.snapshot.model_copy(update={"head_sha": "f" * 40})
    with pytest.raises(ValueError, match="invalid_cursor"):
        find_paths(context, cursor=first["next_cursor"])


async def test_unreadable_earlier_page_and_cursor_restart_preserve_scope(source_service):
    texts = {f"pkg/f{index:03d}.py": "no match\n" for index in range(125)}
    texts["pkg/f000.py"] = "large" * 20
    texts["pkg/f124.py"] = "NEEDLE\nNEEDLE\n"
    commit_sources(source_service, texts)
    source_service.max_file_bytes = 50
    context = context_for(source_service)
    first = await search(context, "NEEDLE", path="pkg", limit=1)
    assert first["has_more"] and first["unreadable_files"] == 1
    # A recreated invocation shares only the immutable archive, not in-memory cursor state.
    restarted = context_for(source_service)
    with pytest.raises(ValueError, match="invalid_cursor"):
        await search(restarted, "NEEDLE", path="pkg", cursor=first["next_cursor"], limit=1)
    last = await search(context, "NEEDLE", path="pkg", cursor=first["next_cursor"], limit=1)
    assert not last["has_more"] and last["matches"][0]["path"] == "pkg/f124.py"
    assert last["unreadable_files"] == 0 and not last["scope_complete"]


async def test_search_cursor_filters_ranges_and_authorization(source_service):
    commit_sources(source_service, {"many.py": "VALUE\n" * 20})
    context = context_for(source_service)
    context.denied_paths = frozenset({"safe.py"})
    first = await search(context, "VALUE", path="many.py")
    for kwargs in ({"query": "OTHER", "path": "many.py"},
                   {"query": "VALUE", "path": "many.py", "side": "base"},
                   {"query": "VALUE", "path": "many.py", "glob": "*.py"}):
        with pytest.raises(ValueError, match="invalid_cursor"):
            await search(context, cursor=first["next_cursor"], **kwargs)
    for cursor in ("!", hashlib.sha256(b'{"column":0,"position":1,"scope":"' +
                                       b"0" * 64 + b'"}').hexdigest()):
        with pytest.raises(ValueError, match="invalid_cursor"):
            await search(context, "VALUE", path="many.py", cursor=cursor)
    for path in ("../safe.py", ".env", "safe.py", "/absolute", "C:/file"):
        with pytest.raises(ValueError):
            await search(context, "VALUE", path=path)
    with pytest.raises(ValueError):
        await search(context, "VALUE", limit=0)
    with pytest.raises(ValueError):
        await search(context, "VALUE", glob="../*")
    with pytest.raises(ValueError):
        await search(context, "VALUE\n")


async def test_large_binary_unreadable_and_empty_scopes_fail_closed(source_service, monkeypatch):
    commit_sources(source_service, {"large.py": "SECRET\n" * 50, "blob.dat": "binary\x00SECRET"})
    source_service.max_file_bytes = 100
    context = context_for(source_service)
    result = await search(context, "SECRET", path="large.py")
    assert result["matches"] == [] and result["unreadable_files"] == 1
    assert not result["scope_complete"]
    result = await search(context, "SECRET", path="blob.dat")
    assert result["matches"] == []
    assert source_service.git.find_matching_paths(source_service.repo_path,
        source_service.snapshot.head_sha, "VALUE", []) == set()
    source_service.max_file_bytes = 512_000
    context.evidence.allowed_paths = frozenset(
        {f"pkg/{index:02d}.py" for index in range(19)} | {"pkg/zz.py"})
    for index in range(19):
        target = source_service.repo_path / f"pkg/{index:02d}.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x" * 450_000, encoding="utf-8")
    target = source_service.repo_path / "pkg/zz.py"
    target.write_text("TARGET\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source_service.repo_path), "add", "."],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(source_service.repo_path), "-c", "user.name=Test",
        "-c", "user.email=test@example.test", "commit", "-m", "large files"],
        check=True, capture_output=True)
    head = subprocess.run(["git", "-C", str(source_service.repo_path), "rev-parse", "HEAD"],
                          check=True, capture_output=True).stdout.decode().strip()
    source_service.snapshot = source_service.snapshot.model_copy(update={"head_sha": head})
    pages = []
    cursor = ""
    while True:
        page = await search(context, "TARGET", path="pkg", cursor=cursor)
        pages.append(page)
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
    assert [match["path"] for page in pages for match in page["matches"]] == ["pkg/zz.py"]
    assert all(page["scope_complete"] is False for page in pages[:-1])
    assert pages[-1]["scope_complete"] is True

    def timeout(*args):
        raise subprocess.TimeoutExpired("git", 1)

    monkeypatch.setattr(source_service.git, "find_matching_paths", timeout)
    with pytest.raises(ValueError, match="snapshot_search_timeout"):
        await search(context, "VALUE")


async def test_search_cancellation_is_propagated(source_service):
    async def cancelled():
        raise asyncio.CancelledError()

    context = context_for(source_service)
    context.check_active = cancelled
    with pytest.raises(asyncio.CancelledError):
        await search(context, "VALUE")
