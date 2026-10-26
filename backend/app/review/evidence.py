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


@dataclass
class EvidenceService:
    snapshot: ReviewSnapshot
    repo_path: Path
    allowed_paths: frozenset[str]
    artifacts: LocalArtifactStore
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
            raise ValueError("invalid_source_side")
        sha = self.snapshot.head_sha if side == "head" else self.snapshot.base_sha
        entry = self.git.get_tree_entries(self.repo_path, sha, [path]).get(path)
        if not entry or entry.get("mode") not in {"100644", "100755"}:
            raise ReviewPathPolicyError("source_not_regular_blob")
        if entry.get("size") is None or entry["size"] > self.max_file_bytes:
            raise ValueError("source_too_large")
        # Existing Head paths must also obey realpath containment. Base deletions
        # are read from blobs and need no working tree file.
        if (self.repo_path / path).exists() or (self.repo_path / path).is_symlink():
            validate_repository_file(self.repo_path, path, require_tracked_if_git=False)
        source = self.git.get_file_content_at_revision(self.repo_path, sha, path)
        if source is None:
            raise ValueError("source_unavailable")
        return sha, source

    def _read(self, path: str, start: int, end: int, side: str) -> dict:
        if type(start) is not int or type(end) is not int or start < 1 or end < start:
            raise ValueError("invalid_source_range")
        if end - start + 1 > self.max_lines:
            raise ValueError("source_range_too_large")
        sha, source = self._source(path, side)
        lines = source.splitlines(keepends=True)
        if end > len(lines):
            raise ValueError("source_range_out_of_bounds")
        content = "".join(lines[start - 1:end])
        if len(content) > self.max_output_chars:
            raise ValueError("source_output_too_large")
        payload = {
            "review_id": self.snapshot.review_id, "commit_sha": sha, "side": side,
            "path": path, "start_line": start, "end_line": end,
            "content_hash": digest(content), "content": content,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        artifact = self.artifacts.put_text(
            task_id=self.snapshot.review_id, kind="evidence", content=encoded)
        ref = EvidenceRef(id=artifact.sha256, **{k: v for k, v in payload.items() if k != "content"})
        return {"ok": True, "evidence": ref.model_dump(), "content": content, "truncated": False}

    async def read(self, path: str, start: int, end: int, side: str = "head") -> dict:
        async with self._semaphore:
            return await asyncio.to_thread(self._read, path, start, end, side)

    def resolve(self, identity: str) -> EvidenceRef:
        if not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise ValueError("invalid_evidence_id")
        path = self.artifacts.root / self.snapshot.review_id / f"evidence-{identity}.txt"
        encoded = self.artifacts.read_text(path.as_uri())
        if digest(encoded) != identity:
            raise ValueError("evidence_content_corrupted")
        payload = json.loads(encoded)
        ref = EvidenceRef(id=identity, **{k: v for k, v in payload.items() if k != "content"})
        sha, source = self._source(ref.path, ref.side)
        original = "".join(source.splitlines(keepends=True)[ref.start_line - 1:ref.end_line])
        if (ref.review_id != self.snapshot.review_id or ref.commit_sha != sha
                or payload["content"] != original or digest(original) != ref.content_hash):
            raise ValueError("evidence_snapshot_mismatch")
        return ref
