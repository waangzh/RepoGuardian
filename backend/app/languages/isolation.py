"""Bounded first-party parser processes and content-bound crash quarantine."""

from __future__ import annotations

import base64
import atexit
import hashlib
import json
import logging
import os
import platform
import queue
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.languages.base import AnalysisLevel, FileAnalysis
from app.languages import parser_cache

logger = logging.getLogger("RepoGuardian.Parser")
PARSER_PROTOCOL = "isolated-parser-v3"
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
PARSER_TIMEOUT_SECONDS = 15.0
MAX_WORKER_REQUESTS = 256
MAX_WORKER_SOURCE_BYTES = 64 * 1024 * 1024
_WORKER = Path(__file__).with_name("parser_worker.py").resolve()
_SERVER_ROOT = Path(__file__).resolve().parents[2]
_SLOTS = threading.BoundedSemaphore(2)
_POOL_LOCK = threading.Lock()
_POOL_GENERATION = 0
_IDLE_WORKERS: list[_ParserWorker] = []
_WORKERS: set[_ParserWorker] = set()
_RECENT_FAILURES: OrderedDict[str, FailureRecord] = OrderedDict()
_FAILURE_LOCK = threading.Lock()
_EXTENSIONS = {
    "python": {".py"}, "javascript": {".js", ".jsx", ".mjs", ".cjs"},
    "typescript": {".ts", ".tsx", ".mts", ".cts"}, "go": {".go"},
    "java": {".java"}, "rust": {".rs"},
}


class ParserCancelled(Exception):
    """Cancellation must propagate through registry fallback handling."""


class ParserFailure(Exception):
    def __init__(self, reason: str, exit_code: int | None = None):
        self.reason = reason
        self.exit_code = exit_code
        super().__init__(reason)


class ParserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    protocol: Literal["isolated-parser-v3"]
    request_id: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[a-f0-9]{32}$")
    language_id: Literal["python", "javascript", "typescript", "go", "java", "rust"]
    relative_path: str = Field(min_length=1, max_length=1000)
    extension: str
    source: str = Field(max_length=((MAX_SOURCE_BYTES + 2) // 3) * 4)

    def source_bytes(self) -> bytes:
        path = PurePosixPath(self.relative_path)
        if (path.is_absolute() or PureWindowsPath(self.relative_path).drive
                or "\\" in self.relative_path or "\x00" in self.relative_path
                or any(part in {"", ".", ".."} for part in self.relative_path.split("/"))
                or self.extension not in _EXTENSIONS[self.language_id]
                or path.suffix.casefold() != self.extension):
            raise ValueError("invalid parser file identity")
        source = base64.b64decode(self.source, validate=True)
        if len(source) > MAX_SOURCE_BYTES:
            raise ValueError("parser source too large")
        return source


class FailureRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    key: str = Field(pattern=r"^[a-f0-9]{64}$")
    relative_path: str
    language_id: str
    source_sha256: str
    parser_fingerprint: str
    reason: str
    exit_code: int | None


def parser_fingerprint() -> str:
    return _parser_fingerprint(PARSER_PROTOCOL)


@lru_cache(maxsize=4)
def _parser_fingerprint(protocol: str) -> str:
    dependencies = {}
    for name in ("tree-sitter", "tree-sitter-python", "tree-sitter-javascript",
                 "tree-sitter-typescript", "tree-sitter-go", "tree-sitter-java", "tree-sitter-rust"):
        try:
            dependencies[name] = version(name)
        except PackageNotFoundError:
            dependencies[name] = "unavailable"
    return _digest({"protocol": protocol, "python": sys.version,
                    "platform": platform.system(), "machine": platform.machine(),
                    "dependencies": dependencies})


def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _failure_directory() -> Path:
    from app.core.config import settings

    return settings.repoguardian_artifact_dir.parent / "parser-failures"


def _load_failure(key: str) -> FailureRecord | None:
    with _FAILURE_LOCK:
        if key in _RECENT_FAILURES:
            return _RECENT_FAILURES[key]
    path = _failure_directory() / f"{key}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("rb") as stream:
            record = FailureRecord.model_validate_json(stream.read(8193))
        if record.key != key:
            raise ValueError("quarantine identity mismatch")
        return record
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ParserFailure("quarantine_unavailable") from exc


def _save_failure(record: FailureRecord) -> bool:
    with _FAILURE_LOCK:
        _RECENT_FAILURES[record.key] = record
        if len(_RECENT_FAILURES) > 256:
            _RECENT_FAILURES.popitem(last=False)
    directory = _failure_directory()
    temporary = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(record.model_dump_json().encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / f"{record.key}.json")
        return True
    except OSError:
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                logger.warning("解析隔离记录的临时文件清理失败")


def _check_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ParserCancelled()


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


class _ParserWorker:
    def __init__(self):
        self.identity = (str(_WORKER), PARSER_PROTOCOL)
        self.requests = 0
        self.source_bytes = 0
        self.closed = False
        self._close_lock = threading.Lock()
        self._io: threading.Thread | None = None
        self.process = subprocess.Popen(
            [sys.executable, "-I", str(_WORKER), "--persistent"],
            cwd=_SERVER_ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={name: value for name, value in os.environ.items()
                 if name.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL"}},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

    def _read(self, size: int) -> bytes:
        output = bytearray()
        while len(output) < size:
            chunk = self.process.stdout.read(size - len(output))
            if not chunk:
                try:
                    code = self.process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    code = self.process.poll()
                raise ParserFailure("output_limit" if code == 3 else "process_exit", code)
            output.extend(chunk)
        return bytes(output)

    def exchange(self, request: ParserRequest, cancel_event: threading.Event | None) -> bytes:
        completed: queue.Queue = queue.Queue(maxsize=1)
        pending = request.model_dump_json().encode("utf-8")

        def transfer():
            try:
                self.process.stdin.write(struct.pack("!I", len(pending)) + pending)
                self.process.stdin.flush()
                size = struct.unpack("!I", self._read(4))[0]
                if size > MAX_OUTPUT_BYTES:
                    raise ParserFailure("output_limit")
                if self._read(32) != request.request_id.encode("ascii"):
                    raise ParserFailure("invalid_response")
                completed.put(self._read(size))
            except (OSError, ValueError, ParserFailure) as exc:
                completed.put(exc)

        self._io = threading.Thread(target=transfer, name="parser-io", daemon=True)
        started = time.monotonic()
        self._io.start()
        while True:
            _check_cancelled(cancel_event)
            remaining = PARSER_TIMEOUT_SECONDS - (time.monotonic() - started)
            if remaining <= 0:
                raise ParserFailure("timeout", self.process.poll())
            try:
                result = completed.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue
            if isinstance(result, ParserFailure):
                raise result
            if isinstance(result, Exception):
                raise ParserFailure("process_exit", self.process.poll()) from result
            self._io.join()
            self.requests += 1
            self.source_bytes += len(request.source)
            return result

    def close(self):
        with self._close_lock:
            if self.closed:
                return
            self.closed = True
            _stop(self.process)
            if self._io is not None:
                self._io.join(timeout=5)
            for stream in (self.process.stdin, self.process.stdout):
                try:
                    stream.close()
                except OSError:
                    pass


def close_parser_workers() -> None:
    global _POOL_GENERATION
    with _POOL_LOCK:
        _POOL_GENERATION += 1
        workers = tuple(_WORKERS)
        _IDLE_WORKERS.clear()
        _WORKERS.clear()
    for worker in workers:
        worker.close()


atexit.register(close_parser_workers)


def _run_worker(request: ParserRequest, cancel_event: threading.Event | None) -> FileAnalysis:
    while not _SLOTS.acquire(timeout=0.1):
        _check_cancelled(cancel_event)
    worker = None
    reusable = False
    try:
        _check_cancelled(cancel_event)
        with _POOL_LOCK:
            generation = _POOL_GENERATION
            worker = _IDLE_WORKERS.pop() if _IDLE_WORKERS else None
        if worker is not None and (
            worker.identity != (str(_WORKER), PARSER_PROTOCOL)
            or worker.process.poll() is not None or worker.closed
        ):
            worker.close()
            with _POOL_LOCK:
                _WORKERS.discard(worker)
            worker = None
        if worker is None:
            worker = _ParserWorker()
            with _POOL_LOCK:
                if generation != _POOL_GENERATION:
                    worker.close()
                    raise ParserCancelled()
                _WORKERS.add(worker)
        raw = worker.exchange(request, cancel_event)
        try:
            result = _validate_result(raw, request, request.source_bytes())
        except ValueError as exc:
            raise ParserFailure("invalid_response", 0) from exc
        _check_cancelled(cancel_event)
        reusable = True
        return result
    except OSError as exc:
        raise ParserFailure("start_failed") from exc
    finally:
        try:
            if worker is not None:
                with _POOL_LOCK:
                    keep = (reusable and generation == _POOL_GENERATION
                            and not worker.closed and worker.process.poll() is None
                            and worker.requests < MAX_WORKER_REQUESTS
                            and worker.source_bytes < MAX_WORKER_SOURCE_BYTES)
                    if keep:
                        _IDLE_WORKERS.append(worker)
                    else:
                        _WORKERS.discard(worker)
                if not keep:
                    worker.close()
        finally:
            _SLOTS.release()


def _validate_result(raw: bytes, request: ParserRequest, source: bytes) -> FileAnalysis:
    result = FileAnalysis.model_validate_json(raw)
    expected = {
        AnalysisLevel.semantic: f"tree-sitter.{request.language_id}.v1",
        AnalysisLevel.heuristic: f"regex.{request.language_id}.v1",
    }.get(result.level)
    lines = max(1, len(source.splitlines()))
    if (result.language_id != request.language_id or result.parser_id != expected
            or (result.level == AnalysisLevel.heuristic and not result.diagnostics)):
        raise ValueError("invalid parser result identity")
    for symbol in result.symbols:
        if (symbol.file != request.relative_path or symbol.parser_id != expected
                or not 1 <= symbol.start_line <= symbol.end_line <= lines):
            raise ValueError("invalid parser symbol")
        for call in symbol.calls:
            if (call.parser_id != expected
                    or (call.line is not None and not 1 <= call.line <= lines)
                    or (result.level == AnalysisLevel.semantic and call.line is None)):
                raise ValueError("invalid parser call")
    if any(item.parser_id != expected or item.line is None or not 1 <= item.line <= lines
           for item in result.imports):
        raise ValueError("invalid parser import")
    return result


def analyze_isolated(adapter, source: bytes, relative_path: str, extension: str,
                     cancel_event: threading.Event | None = None) -> FileAnalysis:
    _check_cancelled(cancel_event)
    if len(source) > MAX_SOURCE_BYTES:
        return FileAnalysis(language_id=adapter.language_id, level=AnalysisLevel.text,
                            parser_id="text.v1", diagnostics=["parser source exceeds 4 MiB; L0 only"])
    request = ParserRequest(protocol=PARSER_PROTOCOL, language_id=adapter.language_id,
                            relative_path=relative_path, extension=extension,
                            source=base64.b64encode(source).decode("ascii"))
    request.source_bytes()
    identity = {"relative_path": relative_path, "language_id": adapter.language_id,
                "source_sha256": hashlib.sha256(source).hexdigest(),
                "parser_fingerprint": parser_fingerprint()}
    key = _digest(identity)
    repeated = False
    persisted = True
    try:
        record = _load_failure(key)
        if record is not None:
            repeated = True
            raise ParserFailure(record.reason, record.exit_code)
        cache_key = _digest({"cache": parser_cache.CACHE_PROTOCOL, **identity})
        cached = parser_cache.load(cache_key)
        if cached is not None:
            try:
                result = _validate_result(cached, request, source)
                if result.level == AnalysisLevel.semantic:
                    _check_cancelled(cancel_event)
                    return result
            except ValueError:
                logger.warning("解析成功缓存校验失败，重新解析 file=%r", relative_path)
        logger.info("解析开始 file=%r language=%s input=%s",
                    relative_path, adapter.language_id, key[:12])
        result = _run_worker(request, cancel_event)
        _check_cancelled(cancel_event)
        if result.level == AnalysisLevel.semantic:
            parser_cache.save(cache_key, result.model_dump_json().encode("utf-8"))
        _check_cancelled(cancel_event)
        return result
    except ParserFailure as exc:
        if not repeated and exc.reason != "quarantine_unavailable":
            persisted = _save_failure(FailureRecord(key=key, **identity,
                                                   reason=exc.reason, exit_code=exc.exit_code))
        code = "unknown" if exc.exit_code is None else f"{exc.exit_code} (0x{exc.exit_code & 0xffffffff:08X})"
        diagnostic = f"isolated parser {exc.reason}; exit_code={code}; downgraded to L1"
        if repeated:
            diagnostic += "; quarantined input, native retry skipped"
        if not persisted:
            diagnostic += "; quarantine persistence failed"
        logger.warning("解析降级 file=%r language=%s input=%s reason=%s exit_code=%s repeated=%s persisted=%s",
                       relative_path, adapter.language_id, key[:12], exc.reason, code, repeated, persisted)
        result = adapter.analyze_source(source, relative_path)
        result.diagnostics.append(diagnostic)
        return result
