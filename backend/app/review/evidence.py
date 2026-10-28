"""Immutable, content addressed source evidence behind all agent tools."""

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.review.models import EvidenceRef, ReviewSnapshot
from app.review.tool_scope import (
    ReviewPathPolicyError,
    _validate_repository_relative_path,
    is_sensitive_repository_path,
    validate_repository_file,
)
from app.services.review.artifact_store import LocalArtifactStore
from app.tools.git_tool import GitTool


def digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def source_lines(content: str) -> list[str]:
    """Git line numbers count LF, not Unicode separators in string literals."""
    lines = content.split("\n")
    return [line + "\n" for line in lines[:-1]] + ([lines[-1]] if lines[-1] else [])


def uncovered_ranges(start: int, end: int, intervals) -> list[tuple[int, int]]:
    missing, cursor = [], start
    for first, last in sorted(intervals):
        if last < cursor:
            continue
        if first > end:
            break
        if first > cursor:
            missing.append((cursor, first - 1))
        cursor = max(cursor, last + 1)
        if cursor > end:
            break
    if cursor <= end:
        missing.append((cursor, end))
    return missing


class SourceReadError(ValueError):
    def __init__(self, code, **details):
        super().__init__(code)
        self.code, self.details = code, details



@dataclass
class EvidenceArchive:
    snapshot: ReviewSnapshot
    artifacts: LocalArtifactStore

    def resolve(self, identity: str) -> EvidenceRef:
        if not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise ValueError("invalid_evidence_id")
        path = self.artifacts.root / self.snapshot.review_id / f"evidence-{identity}.txt"
        encoded = self.artifacts.read_text(path.as_uri())
        if digest(encoded) != identity:
            raise ValueError("evidence_content_corrupted")
        payload = json.loads(encoded)
        ref = EvidenceRef(id=identity, **{k: v for k, v in payload.items() if k != "content"})
        expected = self.snapshot.head_sha if ref.side == "head" else self.snapshot.base_sha
        if (ref.review_id != self.snapshot.review_id or ref.commit_sha != expected
                or digest(payload["content"]) != ref.content_hash
                or len(source_lines(payload["content"])) != ref.end_line - ref.start_line + 1
                or is_sensitive_repository_path(ref.path)):
            raise ValueError("evidence_snapshot_mismatch")
        return ref


@dataclass
class EvidenceService(EvidenceArchive):
    repo_path: Path
    allowed_paths: frozenset[str]
    git: GitTool
    max_lines: int = 240
    max_file_bytes: int = 512_000
    max_output_chars: int = 24_000
    _semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(8))

    def _source(self, path: str, side: str) -> tuple[str, str]:
        _validate_repository_relative_path(path)
        if path not in self.allowed_paths or is_sensitive_repository_path(path):
            raise ReviewPathPolicyError("path_not_authorized")
        if side not in {"head", "base"}:
            raise SourceReadError("invalid_source_side")
        sha = self.snapshot.head_sha if side == "head" else self.snapshot.base_sha
        entry = self.git.get_tree_entries(self.repo_path, sha, [path]).get(path)
        if not entry or entry.get("mode") not in {"100644", "100755"}:
            raise ReviewPathPolicyError("source_not_regular_blob")
        if entry.get("size") is None or entry["size"] > self.max_file_bytes:
            raise SourceReadError("source_too_large")
        # Existing Head paths must also obey realpath containment. Base deletions
        # are read from blobs and need no working tree file.
        if (self.repo_path / path).exists() or (self.repo_path / path).is_symlink():
            validate_repository_file(self.repo_path, path, require_tracked_if_git=False)
        source = self.git.get_file_content_at_revision(self.repo_path, sha, path)
        if source is None:
            raise SourceReadError("source_unavailable")
        return sha, source

    def _read(self, path: str, start: int, end: int, side: str) -> dict:
        if type(start) is not int or type(end) is not int or start < 1 or end < start:
            raise SourceReadError("invalid_source_range")
        if end - start + 1 > self.max_lines:
            raise SourceReadError("source_range_too_large", max_lines=self.max_lines,
                requested_range=[start, end], requested_lines=end - start + 1,
                suggested_range=[start, start + self.max_lines - 1],
                next_start=start + self.max_lines, retryable_with_corrected_arguments=True)
        sha, source = self._source(path, side)
        lines = source_lines(source)
        if end > len(lines):
            raise SourceReadError("source_range_out_of_bounds", side=side,
                requested_range=[start, end], available_range=[1, len(lines)] if lines else None,
                retryable_with_corrected_arguments=bool(lines))
        content = "".join(lines[start - 1:end])
        if len(content) > self.max_output_chars:
            raise SourceReadError("source_output_too_large")
        payload = {
            "review_id": self.snapshot.review_id, "commit_sha": sha, "side": side,
            "path": path, "start_line": start, "end_line": end,
            "content_hash": digest(content), "content": content,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        artifact = self.artifacts.put_text(
            task_id=self.snapshot.review_id, kind="evidence", content=encoded)
        ref = EvidenceRef(id=artifact.sha256, **{k: v for k, v in payload.items() if k != "content"})
        return {"ok": True, "evidence": ref.model_dump(), "content": content, "truncated": False,
                "total_lines": len(lines), "max_read_lines": self.max_lines}

    async def read(self, path: str, start: int, end: int, side: str = "head") -> dict:
        async with self._semaphore:
            return await asyncio.to_thread(self._read, path, start, end, side)

    def resolve(self, identity: str) -> EvidenceRef:
        ref = super().resolve(identity)
        sha, source = self._source(ref.path, ref.side)
        original = "".join(source_lines(source)[ref.start_line - 1:ref.end_line])
        if ref.commit_sha != sha or digest(original) != ref.content_hash:
            raise ValueError("evidence_snapshot_mismatch")
        return ref
