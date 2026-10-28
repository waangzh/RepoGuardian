import hashlib
import json
import sqlite3
import subprocess
import sys
import threading

import pytest

from app.languages import isolation, parser_cache
from app.languages.base import AnalysisLevel, FileAnalysis
from app.languages.registry import default_language_registry


def source_file(tmp_path, name="module.py"):
    path = tmp_path / name
    path.write_text("import math\ndef entry(x): return math.sqrt(x)\n", encoding="utf-8")
    return path


def analyze(path, **kwargs):
    return default_language_registry.analyze(path, path.name, **kwargs)


def test_success_survives_new_interpreter(tmp_path, monkeypatch):
    path = source_file(tmp_path)
    result = analyze(path)
    assert result.level == AnalysisLevel.semantic
    cache_path = parser_cache._cache_path()
    code = (
        "import sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(isolation._SERVER_ROOT)!r})\n"
        "from app.languages import isolation, parser_cache\n"
        "from app.languages.registry import default_language_registry\n"
        f"parser_cache._cache_path=lambda:Path({str(cache_path)!r})\n"
        f"isolation._failure_directory=lambda:Path({str(cache_path.parent / 'failures')!r})\n"
        "def forbidden(*args, **kwargs): raise AssertionError('cached input launched worker')\n"
        "isolation._run_worker=forbidden\n"
        f"p=Path({str(path)!r})\n"
        "print(default_language_registry.analyze(p,p.name).model_dump_json())\n"
    )
    child = subprocess.run([sys.executable, "-I", "-c", code],
                           capture_output=True, timeout=15, check=True)
    assert FileAnalysis.model_validate_json(child.stdout) == result


@pytest.mark.parametrize("change", ["source", "path", "parser"])
def test_content_identity_change_misses_success_cache(tmp_path, monkeypatch, change):
    path = source_file(tmp_path)
    assert analyze(path).level == AnalysisLevel.semantic
    calls = []
    original = isolation._run_worker

    def run(*args, **kwargs):
        calls.append(args[0].relative_path)
        return original(*args, **kwargs)

    monkeypatch.setattr(isolation, "_run_worker", run)
    if change == "source":
        path.write_text("def updated(): pass\n", encoding="utf-8")
    elif change == "path":
        renamed = tmp_path / "renamed.py"
        path.rename(renamed)
        path = renamed
    else:
        monkeypatch.setattr(isolation, "parser_fingerprint", lambda: "replacement-parser")
    assert analyze(path).level == AnalysisLevel.semantic
    assert calls == [path.name]


@pytest.mark.parametrize("fault", ["digest", "schema", "identity", "database"])
def test_damaged_success_cache_recomputes_instead_of_returning_bad_data(
    tmp_path, monkeypatch, fault,
):
    path = source_file(tmp_path)
    expected = analyze(path)
    cache_path = parser_cache._cache_path()
    if fault == "database":
        cache_path.write_bytes(b"not a database")
    else:
        with sqlite3.connect(cache_path) as db:
            if fault == "digest":
                db.execute("UPDATE analyses SET digest = 'bad'")
            else:
                payload = json.loads(db.execute("SELECT payload FROM analyses").fetchone()[0])
                if fault == "schema":
                    payload["invalid"] = "extra"
                else:
                    payload["symbols"][0]["file"] = "other.py"
                raw = json.dumps(payload).encode()
                db.execute("UPDATE analyses SET payload=?, digest=?, size=?",
                           (raw, hashlib.sha256(raw).hexdigest(), len(raw)))
    calls = []
    original = isolation._run_worker

    def run(*args, **kwargs):
        calls.append(args[0].relative_path)
        return original(*args, **kwargs)

    monkeypatch.setattr(isolation, "_run_worker", run)
    assert analyze(path) == expected
    assert calls == [path.name]


def test_unavailable_cache_does_not_downgrade_valid_analysis(tmp_path, monkeypatch):
    path = source_file(tmp_path)
    blocker = tmp_path / "unavailable"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(parser_cache, "_cache_path", lambda: blocker / "cache.db")
    assert analyze(path).level == AnalysisLevel.semantic


def test_failed_or_cancelled_results_are_not_saved(tmp_path, monkeypatch):
    path = source_file(tmp_path)
    monkeypatch.setattr(isolation, "_failure_directory", lambda: tmp_path / "quarantine")
    original = isolation._run_worker

    def crash(*args, **kwargs):
        raise isolation.ParserFailure("process_exit", 23)

    monkeypatch.setattr(isolation, "_run_worker", crash)
    assert analyze(path).level == AnalysisLevel.heuristic
    assert not parser_cache._cache_path().exists()
    monkeypatch.setattr(isolation, "_run_worker", original)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(isolation.ParserCancelled):
        analyze(source_file(tmp_path, "cancelled.py"), cancel_event=cancel)
    assert not parser_cache._cache_path().exists()


def test_quarantine_takes_precedence_over_success_cache(tmp_path, monkeypatch):
    path = source_file(tmp_path)
    assert analyze(path).level == AnalysisLevel.semantic
    record = isolation.FailureRecord(
        key="a" * 64, relative_path=path.name, language_id="python",
        source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        parser_fingerprint=isolation.parser_fingerprint(), reason="timeout", exit_code=None,
        attempts=[isolation.FailureAttempt(reason="timeout", exit_code=None)],
    )
    monkeypatch.setattr(isolation, "_load_failure", lambda key: record)
    result = analyze(path)
    assert result.level == AnalysisLevel.heuristic
    assert "quarantined input" in result.diagnostics[0]


def test_cache_capacity_and_entry_size_are_bounded(monkeypatch):
    monkeypatch.setattr(parser_cache, "MAX_CACHE_ENTRIES", 2)
    monkeypatch.setattr(parser_cache, "MAX_CACHE_BYTES", 8)
    monkeypatch.setattr(parser_cache, "MAX_ENTRY_BYTES", 5)
    parser_cache.save("a", b"1234")
    parser_cache.save("b", b"5678")
    assert parser_cache.load("a") == b"1234"
    parser_cache.save("c", b"abcd")
    assert parser_cache.load("a") is None
    assert parser_cache.load("c") == b"abcd"
    parser_cache.save("oversized", b"123456")
    assert parser_cache.load("oversized") is None
    monkeypatch.setattr(parser_cache, "MAX_CACHE_BYTES", 4)
    parser_cache.save("d", b"1234")
    assert parser_cache.load("b") is None and parser_cache.load("c") is None
    assert parser_cache.load("d") == b"1234"
    monkeypatch.setattr(parser_cache, "MAX_ENTRY_BYTES", 3)
    assert parser_cache.load("d") is None


async def test_interrupted_index_reuses_completed_files_on_retry(tmp_path, monkeypatch):
    from app.tools.repo_indexer import RepoIndexer

    source_file(tmp_path, "a.py")
    source_file(tmp_path, "b.py")
    original = isolation._run_worker

    def interrupt(request, cancel, **kwargs):
        if request.relative_path == "b.py":
            cancel.set()
            raise isolation.ParserCancelled()
        return original(request, cancel, **kwargs)

    monkeypatch.setattr(isolation, "_run_worker", interrupt)
    with pytest.raises(isolation.ParserCancelled):
        await RepoIndexer().execute(repo_path=str(tmp_path))
    calls = []

    def resume(request, cancel, **kwargs):
        calls.append(request.relative_path)
        return original(request, cancel, **kwargs)

    monkeypatch.setattr(isolation, "_run_worker", resume)
    result = await RepoIndexer().execute(repo_path=str(tmp_path))
    assert calls == ["b.py"]
    assert len(result["file_index"]) == len(result["symbol_index"]) == 2
