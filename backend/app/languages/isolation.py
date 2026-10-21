"""Bounded first-party parser processes and content-bound crash quarantine."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.languages.base import AnalysisLevel, FileAnalysis

logger = logging.getLogger("RepoGuardian.Parser")
PARSER_PROTOCOL = "isolated-parser-v1"
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
PARSER_TIMEOUT_SECONDS = 15.0
_WORKER = Path(__file__).with_name("parser_worker.py").resolve()
_SERVER_ROOT = Path(__file__).resolve().parents[2]
_SLOTS = threading.BoundedSemaphore(2)
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

    protocol: Literal["isolated-parser-v1"]
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
    dependencies = {}
    for name in ("tree-sitter", "tree-sitter-python", "tree-sitter-javascript",
                 "tree-sitter-typescript", "tree-sitter-go", "tree-sitter-java", "tree-sitter-rust"):
        try:
            dependencies[name] = version(name)
        except PackageNotFoundError:
            dependencies[name] = "unavailable"
    return _digest({"protocol": PARSER_PROTOCOL, "python": sys.version,
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


def _run_worker(request: ParserRequest, cancel_event: threading.Event | None) -> bytes:
    while not _SLOTS.acquire(timeout=0.1):
        _check_cancelled(cancel_event)
    try:
        _check_cancelled(cancel_event)
        # The child sees only server code/dependencies and source bytes, never a target cwd.
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                [sys.executable, "-I", str(_WORKER)],
                cwd=_SERVER_ROOT, stdin=subprocess.PIPE, stdout=output,
                stderr=subprocess.DEVNULL,
                env={name: value for name, value in os.environ.items()
                     if name.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL"}},
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            try:
                started = time.monotonic()
                pending = request.model_dump_json().encode("utf-8")
                while True:
                    _check_cancelled(cancel_event)
                    if time.monotonic() - started >= PARSER_TIMEOUT_SECONDS:
                        _stop(process)
                        raise ParserFailure("timeout", process.returncode)
                    if os.fstat(output.fileno()).st_size > MAX_OUTPUT_BYTES:
                        _stop(process)
                        raise ParserFailure("output_limit", process.returncode)
                    try:
                        process.communicate(pending, timeout=0.1)
                        break
                    except subprocess.TimeoutExpired:
                        pending = None
                if process.returncode:
                    reason = "output_limit" if process.returncode == 3 else "process_exit"
                    raise ParserFailure(reason, process.returncode)
                output.seek(0)
                result = output.read(MAX_OUTPUT_BYTES + 1)
                if len(result) > MAX_OUTPUT_BYTES:
                    raise ParserFailure("output_limit", process.returncode)
                return result
            finally:
                _stop(process)
                if process.stdin is not None:
                    process.stdin.close()
    except OSError as exc:
        raise ParserFailure("start_failed") from exc
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
        logger.info("解析开始 file=%r language=%s input=%s",
                    relative_path, adapter.language_id, key[:12])
        raw = _run_worker(request, cancel_event)
        _check_cancelled(cancel_event)
        try:
            return _validate_result(raw, request, source)
        except ValueError as exc:
            raise ParserFailure("invalid_response", 0) from exc
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
