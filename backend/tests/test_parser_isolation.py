import asyncio
import json
import subprocess
import sys
import threading

import pytest

from app.languages import isolation
from app.languages.base import AnalysisLevel, FileAnalysis
from app.languages.registry import default_language_registry
from app.tools.repo_indexer import RepoIndexer


@pytest.fixture(autouse=True)
def quarantine(tmp_path, monkeypatch):
    isolation.close_parser_workers()
    directory = tmp_path / "quarantine"
    monkeypatch.setattr(isolation, "_failure_directory", lambda: directory)
    isolation._RECENT_FAILURES.clear()
    yield directory
    isolation.close_parser_workers()
    isolation._RECENT_FAILURES.clear()


def source_file(tmp_path, name="lib.rs"):
    path = tmp_path / name
    path.write_text("pub fn entry() { helper(); }\n", encoding="utf-8")
    return path


def worker_script(tmp_path, monkeypatch, body):
    worker = tmp_path / "trusted_test_worker.py"
    worker.write_text(body, encoding="utf-8")
    monkeypatch.setattr(isolation, "_WORKER", worker)
    return worker


def analyze(path, **kwargs):
    return default_language_registry.analyze(path, path.name, **kwargs)


def test_real_process_exit_is_l1_and_quarantined(tmp_path, monkeypatch, quarantine, caplog):
    path = source_file(tmp_path)
    worker_script(tmp_path, monkeypatch, "import os\nos._exit(23)\n")
    result = analyze(path)
    assert result.level == AnalysisLevel.heuristic
    assert result.parser_id == "regex.rust.v1"
    assert all(symbol.confidence < 0.9 for symbol in result.symbols)
    assert "process_exit" in result.diagnostics[0]
    assert "exit_code=23 (0x00000017)" in result.diagnostics[0]
    record = json.loads(next(quarantine.glob("*.json")).read_text(encoding="utf-8"))
    assert record["relative_path"] == "lib.rs" and record["exit_code"] == 23
    assert "pub fn" not in json.dumps(record)
    assert "lib.rs" in caplog.text and "exit_code=23" in caplog.text

    def forbidden(*args, **kwargs):
        pytest.fail("quarantined input must not launch another native process")

    monkeypatch.setattr(isolation, "_run_worker", forbidden)
    result = analyze(path)
    assert result.level == AnalysisLevel.heuristic
    assert "quarantined input" in result.diagnostics[0]


def test_quarantine_survives_a_new_interpreter(tmp_path, monkeypatch, quarantine):
    path = source_file(tmp_path)
    marker = tmp_path / "attempts"
    worker = worker_script(tmp_path, monkeypatch,
                           f"from pathlib import Path\nimport os\n"
                           f"p=Path({str(marker)!r})\n"
                           "p.write_text(p.read_text()+'x' if p.exists() else 'x')\n"
                           "os._exit(29)\n")
    assert analyze(path).level == AnalysisLevel.heuristic
    code = (
        "import sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(isolation._SERVER_ROOT)!r})\n"
        "from app.languages import isolation\n"
        "from app.languages.registry import default_language_registry\n"
        f"isolation._failure_directory=lambda:Path({str(quarantine)!r})\n"
        f"isolation._WORKER=Path({str(worker)!r})\n"
        f"p=Path({str(path)!r})\n"
        "r=default_language_registry.analyze(p,p.name)\n"
        "print(r.model_dump_json())\n"
    )
    result = subprocess.run([sys.executable, "-I", "-c", code],
                            capture_output=True, timeout=15, check=True)
    recovered = FileAnalysis.model_validate_json(result.stdout)
    assert recovered.level == AnalysisLevel.heuristic
    assert "exit_code=29" in recovered.diagnostics[0]
    assert "quarantined input" in recovered.diagnostics[0]
    assert marker.read_text() == "x"


@pytest.mark.parametrize("change", ["source", "parser"])
def test_changed_content_or_parser_can_retry(tmp_path, monkeypatch, change):
    path = source_file(tmp_path)
    original = isolation._WORKER
    worker_script(tmp_path, monkeypatch, "import os\nos._exit(19)\n")
    assert analyze(path).level == AnalysisLevel.heuristic
    monkeypatch.setattr(isolation, "_WORKER", original)
    if change == "source":
        path.write_text("pub fn updated() {}\n", encoding="utf-8")
    else:
        monkeypatch.setattr(isolation, "parser_fingerprint", lambda: "replacement-parser")
    result = analyze(path)
    assert result.level == AnalysisLevel.semantic, result.diagnostics


@pytest.mark.parametrize("failure", ["timeout", "output_limit"])
def test_bounded_worker_is_reaped(tmp_path, monkeypatch, failure):
    path = source_file(tmp_path)
    if failure == "timeout":
        worker_script(tmp_path, monkeypatch, "import time\ntime.sleep(30)\n")
        monkeypatch.setattr(isolation, "PARSER_TIMEOUT_SECONDS", 0.2)
    else:
        worker_script(tmp_path, monkeypatch,
                      "import sys\nsys.stdout.buffer.write(b'x'*8192)\n")
        monkeypatch.setattr(isolation, "MAX_OUTPUT_BYTES", 512)
    processes = []
    original = subprocess.Popen

    def record_process(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(isolation.subprocess, "Popen", record_process)
    result = analyze(path)
    assert result.level == AnalysisLevel.heuristic
    assert failure in result.diagnostics[0]
    assert len(processes) == 1 and processes[0].poll() is not None


def test_cancellation_propagates_and_does_not_quarantine(tmp_path, monkeypatch, quarantine):
    path = source_file(tmp_path)
    worker_script(tmp_path, monkeypatch, "import time\ntime.sleep(30)\n")
    cancel = threading.Event()
    processes = []
    original = subprocess.Popen

    def record_process(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(isolation.subprocess, "Popen", record_process)
    timer = threading.Timer(0.3, cancel.set)
    timer.start()
    try:
        with pytest.raises(isolation.ParserCancelled):
            analyze(path, cancel_event=cancel)
    finally:
        timer.cancel()
        timer.join()
    assert len(processes) == 1 and processes[0].poll() is not None
    assert not list(quarantine.glob("*.json"))


@pytest.mark.parametrize("fault", ["json", "language", "path", "range", "level"])
def test_invalid_worker_results_downgrade(tmp_path, monkeypatch, fault):
    path = source_file(tmp_path)
    raw = {
        "language_id": "rust", "level": 2, "parser_id": "tree-sitter.rust.v1",
        "symbols": [{"file": path.name, "name": "entry", "kind": "function",
                     "start_line": 1, "end_line": 1, "confidence": 0.98,
                     "parser_id": "tree-sitter.rust.v1"}],
    }
    if fault == "language":
        raw["language_id"] = "python"
    elif fault == "path":
        raw["symbols"][0]["file"] = "../outside.rs"
    elif fault == "range":
        raw["symbols"][0]["end_line"] = 9000
    elif fault == "level":
        raw["level"] = 0
    payload = b"invalid" if fault == "json" else json.dumps(raw).encode()
    worker_script(
        tmp_path, monkeypatch,
        "import json, struct, sys\n"
        "size=struct.unpack('!I', sys.stdin.buffer.read(4))[0]\n"
        "request=json.loads(sys.stdin.buffer.read(size))\n"
        f"payload={payload!r}\n"
        "sys.stdout.buffer.write(struct.pack('!I', len(payload))"
        "+request['request_id'].encode('ascii')+payload)\n",
    )
    result = analyze(path)
    assert result.level == AnalysisLevel.heuristic
    assert "invalid_response" in result.diagnostics[0]


@pytest.mark.parametrize("fault", [
    {"relative_path": "../escape.rs"}, {"relative_path": "C:/escape.rs"},
    {"extension": ".py"}, {"language_id": "unknown"}, {"command": "arbitrary"},
])
def test_worker_rejects_invalid_requests(fault):
    request = {
        "protocol": isolation.PARSER_PROTOCOL, "language_id": "rust",
        "relative_path": "lib.rs", "extension": ".rs", "source": "",
    }
    request.update(fault)
    result = subprocess.run([sys.executable, "-I", str(isolation._WORKER)],
                            input=json.dumps(request).encode(), capture_output=True, timeout=10)
    assert result.returncode == 2
    assert result.stdout == b""


def test_target_modules_and_source_are_never_executed(tmp_path, monkeypatch):
    marker = tmp_path / "executed"
    trap = f"from pathlib import Path\nPath({str(marker)!r}).touch()\nraise RuntimeError()\n"
    for name in ("sitecustomize.py", "tree_sitter.py", "target.py"):
        (tmp_path / name).write_text(trap, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    result = analyze(tmp_path / "target.py")
    assert result.level == AnalysisLevel.semantic, result.diagnostics
    assert not marker.exists()


def test_oversized_source_is_explicit_l0(tmp_path, monkeypatch):
    path = tmp_path / "big.rs"
    path.write_bytes(b"x" * (isolation.MAX_SOURCE_BYTES + 1))
    monkeypatch.setattr(isolation, "_run_worker", lambda *args: pytest.fail("oversized input"))
    result = analyze(path)
    assert result.level == AnalysisLevel.text and result.diagnostics


def test_damaged_quarantine_fails_closed(tmp_path, monkeypatch, quarantine):
    path = source_file(tmp_path)
    worker_script(tmp_path, monkeypatch, "import os\nos._exit(23)\n")
    analyze(path)
    isolation._RECENT_FAILURES.clear()
    next(quarantine.glob("*.json")).write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(isolation, "_run_worker", lambda *args: pytest.fail("damaged quarantine"))
    result = analyze(path)
    assert result.level == AnalysisLevel.heuristic
    assert "quarantine_unavailable" in result.diagnostics[0]


def test_unavailable_quarantine_skips_native_parser(tmp_path, monkeypatch, quarantine):
    path = source_file(tmp_path)
    quarantine.write_text("directory unavailable", encoding="utf-8")
    monkeypatch.setattr(isolation, "_run_worker", lambda *args: pytest.fail("unavailable quarantine"))
    result = analyze(path)
    assert result.level == AnalysisLevel.heuristic
    assert "quarantine_unavailable" in result.diagnostics[0]


@pytest.mark.asyncio
async def test_async_index_cancellation_reaps_worker(tmp_path, monkeypatch, quarantine):
    source_file(tmp_path)
    worker_script(tmp_path, monkeypatch, "import time\ntime.sleep(30)\n")
    started, reaped = threading.Event(), threading.Event()
    original_spawn, original_stop = subprocess.Popen, isolation._stop
    processes = []

    def spawn(*args, **kwargs):
        process = original_spawn(*args, **kwargs)
        if args[0][0] == sys.executable:
            processes.append(process)
            started.set()
        return process

    def stop(process):
        original_stop(process)
        reaped.set()

    monkeypatch.setattr(isolation.subprocess, "Popen", spawn)
    monkeypatch.setattr(isolation, "_stop", stop)
    task = asyncio.create_task(RepoIndexer().execute(repo_path=str(tmp_path)))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(reaped.wait, 5)
    assert processes[0].poll() is not None
    assert not list(quarantine.glob("*.json"))


@pytest.mark.asyncio
async def test_crashing_file_does_not_block_index_and_is_not_parsed_twice(tmp_path, monkeypatch):
    source_file(tmp_path, "boom.rs")
    (tmp_path / "good.py").write_text("def good():\n    return 1\n", encoding="utf-8")
    original_worker = isolation._WORKER
    crash = worker_script(tmp_path, monkeypatch, "import os\nos._exit(23)\n")
    monkeypatch.setattr(isolation, "_WORKER", original_worker)
    original_run = isolation._run_worker
    calls = []

    def run(request, cancel):
        calls.append(request.relative_path)
        with monkeypatch.context() as scoped:
            if request.relative_path == "boom.rs":
                scoped.setattr(isolation, "_WORKER", crash)
            return original_run(request, cancel)

    monkeypatch.setattr(isolation, "_run_worker", run)
    result = await RepoIndexer().execute(repo_path=str(tmp_path))
    files = {item["path"]: item for item in result["file_index"]}
    assert files["boom.rs"]["analysis_level"] == 1
    assert "exit_code=23" in files["boom.rs"]["diagnostics"][0]
    assert files["good.py"]["analysis_level"] == 2
    assert any(item["symbol"] == "good" for item in result["symbol_index"])
    assert calls.count("boom.rs") == calls.count("good.py") == 1


def test_rust_large_line_numbers_and_chained_calls(tmp_path):
    path = tmp_path / "large.rs"
    text = "\n" * 270 + "\n".join(
        f"pub fn f{i}() {{ let x = target.load().check(); helper(x); }}" for i in range(50))
    path.write_text(text, encoding="utf-8")
    result = analyze(path)
    assert result.level == AnalysisLevel.semantic, result.diagnostics
    assert len(result.symbols) == 50 and not result.diagnostics
    assert result.symbols[0].start_line == 271
    assert result.symbols[-1].calls[-1].line == 320


def test_parser_version_changes_preview_cache_key(monkeypatch):
    from types import SimpleNamespace
    from app.services import preview_input_cache as cache

    pr = SimpleNamespace(clone_url="https://example.test/repo",
                         base=SimpleNamespace(sha="a" * 40), head=SimpleNamespace(sha="b" * 40))
    configuration = {"call_resolution_version": "test"}
    original = cache.input_cache_key(pr, configuration)
    monkeypatch.setattr(cache, "parser_fingerprint", lambda: "other-parser")
    assert cache.input_cache_key(pr, configuration) != original


def test_parser_version_changes_confirmation_policy(monkeypatch):
    from app.services import preview_confirmation as confirmation

    original = confirmation.policy_fingerprint()
    monkeypatch.setattr(isolation, "parser_fingerprint", lambda: "other-parser")
    assert confirmation.policy_fingerprint() != original


def test_persistent_worker_reuses_process_and_keeps_file_identity(tmp_path, monkeypatch):
    processes = []
    original = subprocess.Popen

    def spawn(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        assert args[0][1] == "-I" and args[0][-1] == "--persistent"
        assert kwargs["cwd"] == isolation._SERVER_ROOT
        assert not {"PYTHONPATH", "API_KEY"} & kwargs["env"].keys()
        return process

    monkeypatch.setattr(isolation.subprocess, "Popen", spawn)
    for i in range(4):
        path = source_file(tmp_path, f"lib{i}.rs")
        result = analyze(path)
        assert result.level == AnalysisLevel.semantic, result.diagnostics
        assert result.symbols[0].file == path.name
    assert len(processes) == 1
    assert processes[0].poll() is None
    isolation.close_parser_workers()
    assert processes[0].poll() is not None


def test_workers_recycle_after_request_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(isolation, "MAX_WORKER_REQUESTS", 2)
    processes = []
    original = subprocess.Popen

    def spawn(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(isolation.subprocess, "Popen", spawn)
    for i in range(3):
        assert analyze(source_file(tmp_path, f"lib{i}.rs")).level == AnalysisLevel.semantic
    assert len(processes) == 2
    assert processes[0].poll() is not None
    assert processes[1].poll() is None


@pytest.mark.parametrize("fault", ["identity", "truncated", "blocked_stdin"])
def test_bad_stream_or_blocked_stdin_is_reaped_and_next_file_recovers(
    tmp_path, monkeypatch, fault,
):
    original_worker = isolation._WORKER
    path = source_file(tmp_path)
    if fault == "identity":
        body = "import struct, sys\nsys.stdout.buffer.write(struct.pack('!I', 2)+b'x'*32+b'{}')\n"
    elif fault == "truncated":
        body = "import sys\nsys.stdout.buffer.write(b'xx')\n"
    else:
        body = "import time\ntime.sleep(30)\n"
        path.write_text("//" + "x" * 512000, encoding="utf-8")
        monkeypatch.setattr(isolation, "PARSER_TIMEOUT_SECONDS", 0.2)
    worker_script(tmp_path, monkeypatch, body)
    result = analyze(path)
    assert result.level == AnalysisLevel.heuristic
    expected = {"identity": "invalid_response", "truncated": "process_exit",
                "blocked_stdin": "timeout"}[fault]
    assert expected in result.diagnostics[0]
    assert not [thread for thread in threading.enumerate() if thread.name == "parser-io"]
    monkeypatch.setattr(isolation, "_WORKER", original_worker)
    monkeypatch.setattr(isolation, "PARSER_TIMEOUT_SECONDS", 15.0)
    assert analyze(source_file(tmp_path, "next.rs")).level == AnalysisLevel.semantic


def test_concurrent_parsing_uses_at_most_two_processes(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    processes = []
    original = subprocess.Popen

    def spawn(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(isolation.subprocess, "Popen", spawn)
    paths = [source_file(tmp_path, f"file{i}.rs") for i in range(6)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(analyze, paths))
    assert 1 <= len(processes) <= 2
    assert all(result.level == AnalysisLevel.semantic for result in results)
    assert [result.symbols[0].file for result in results] == [path.name for path in paths]


def test_cancellation_while_waiting_for_worker_slot_does_not_quarantine(
    tmp_path, quarantine,
):
    path = source_file(tmp_path)
    cancel = threading.Event()
    isolation._SLOTS.acquire()
    isolation._SLOTS.acquire()
    timer = threading.Timer(0.2, cancel.set)
    timer.start()
    try:
        with pytest.raises(isolation.ParserCancelled):
            analyze(path, cancel_event=cancel)
    finally:
        isolation._SLOTS.release()
        isolation._SLOTS.release()
        timer.cancel()
        timer.join()
    assert not list(quarantine.glob("*.json"))


def test_shutdown_during_worker_creation_does_not_leave_process_in_pool(
    tmp_path, monkeypatch, quarantine,
):
    original = subprocess.Popen
    processes = []

    def spawn(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        isolation.close_parser_workers()
        return process

    monkeypatch.setattr(isolation.subprocess, "Popen", spawn)
    with pytest.raises(isolation.ParserCancelled):
        analyze(source_file(tmp_path))
    assert len(processes) == 1 and processes[0].poll() is not None
    assert not isolation._WORKERS and not isolation._IDLE_WORKERS
    assert not list(quarantine.glob("*.json"))
