"""将模型代码片段确定性解析为可信行号和评论位置。"""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Callable, Iterable

from app.evidence.diff_index import DiffIndex, IndexedLine
from app.models.review import (
    CommentPlacement,
    EvidenceAnchor,
    EvidenceCandidate,
    EvidenceResolutionMethod,
    IssueStatus,
    ReviewIssue,
    ReviewUnit,
)

FileLoader = Callable[[str, str], str | None]

_BINARY_EXTENSIONS = {
    ".7z", ".a", ".avi", ".bin", ".bmp", ".class", ".dll", ".dylib",
    ".exe", ".gif", ".gz", ".ico", ".jar", ".jpeg", ".jpg", ".mov",
    ".mp3", ".mp4", ".o", ".pdf", ".png", ".pyc", ".so", ".tar",
    ".ttf", ".wav", ".webm", ".woff", ".woff2", ".xz", ".zip",
}


class EvidenceResolver:
    """严格按 hunk、diff、完整文件、symbol 的顺序解析证据。"""

    def __init__(
        self,
        diff_index: DiffIndex,
        *,
        file_loader: FileLoader,
        symbol_index: list[dict] | None = None,
        repository_files: set[str] | None = None,
    ) -> None:
        self.diff_index = diff_index
        self.file_loader = file_loader
        self.symbol_index = symbol_index or []
        self.repository_files = repository_files

    def resolve_issue(self, issue: ReviewIssue, unit: ReviewUnit) -> ReviewIssue:
        primary = issue.primary_evidence
        readable = set(unit.primary_files) | set(unit.related_files)
        if primary.file_path not in set(unit.primary_files):
            return self._route_invalid(issue, "primary_evidence_not_in_primary_files")
        if any(anchor.file_path not in readable for anchor in issue.supporting_evidence):
            return self._route_invalid(issue, "supporting_evidence_out_of_scope")
        if self._normalize_code(primary.existing_code) == self._normalize_code(issue.recommendation):
            return self._route_invalid(issue, "existing_code_matches_recommendation")

        resolved_primary = self.resolve_anchor(primary)
        supporting = [self.resolve_anchor(anchor) for anchor in issue.supporting_evidence]
        placement, reason = self._placement(resolved_primary)
        status = {
            CommentPlacement.inline: IssueStatus.evidence_resolved,
            CommentPlacement.summary: IssueStatus.evidence_resolved,
            CommentPlacement.needs_human: IssueStatus.needs_human,
            CommentPlacement.suppressed: IssueStatus.dismissed,
        }[placement]
        return issue.model_copy(update={
            "primary_evidence": resolved_primary,
            "supporting_evidence": supporting,
            "placement": placement,
            "status": status,
            "unresolved_reason": reason,
            "requires_human_confirmation": (
                issue.requires_human_confirmation or placement == CommentPlacement.needs_human
            ),
            "auto_fix_eligible": (
                issue.auto_fix_eligible
                and placement in {CommentPlacement.inline, CommentPlacement.summary}
                and not issue.requires_human_confirmation
            ),
        })

    def resolve_anchor(self, anchor: EvidenceAnchor) -> EvidenceAnchor:
        item = self.diff_index.file_for_path(anchor.file_path)
        if self._is_binary(anchor.file_path, item):
            return self._unresolved(anchor, "binary_file", [])
        if item is None and self.repository_files is not None and anchor.file_path not in self.repository_files:
            return self._unresolved(anchor, "file_out_of_scope", [])

        sides = [anchor.expected_side] if anchor.expected_side != "either" else ["head", "base"]
        expected_hunk = anchor.expected_hunk_id
        if expected_hunk:
            hunk = self.diff_index.hunk(anchor.file_path, expected_hunk)
            if hunk is not None:
                for normalized, method in (
                    (False, EvidenceResolutionMethod.diff_exact),
                    (True, EvidenceResolutionMethod.diff_normalized),
                ):
                    candidates = self._search_hunks(anchor, sides, [hunk], normalized)
                    if candidates:
                        return self._finish_candidates(anchor, method, candidates)

        other_hunks = [
            hunk for hunk in self.diff_index.hunks(anchor.file_path)
            if not expected_hunk or hunk.hunk_id != expected_hunk
        ]
        for normalized, method in (
            (False, EvidenceResolutionMethod.diff_exact),
            (True, EvidenceResolutionMethod.diff_normalized),
        ):
            candidates = self._search_hunks(anchor, sides, other_hunks, normalized)
            if candidates:
                return self._finish_candidates(anchor, method, candidates)

        file_candidates = self._search_files(anchor, sides, normalized=False)
        if len(file_candidates) == 1:
            return self._finish_candidates(
                anchor, EvidenceResolutionMethod.file_exact, file_candidates
            )

        if anchor.symbol:
            symbol_candidates = self._symbol_candidates(anchor, sides, file_candidates)
            if symbol_candidates:
                return self._finish_candidates(
                    anchor, EvidenceResolutionMethod.symbol_assisted, symbol_candidates
                )

        if file_candidates:
            return self._unresolved(anchor, "multiple_matches", file_candidates)

        opposite = [side for side in ("head", "base") if side not in sides]
        opposite_candidates = self._search_hunks(
            anchor, opposite, self.diff_index.hunks(anchor.file_path), True
        ) + self._search_files(anchor, opposite, normalized=False)
        if opposite_candidates:
            return self._unresolved(anchor, "side_conflict", opposite_candidates)
        if expected_hunk and self.diff_index.hunk(anchor.file_path, expected_hunk) is None:
            return self._unresolved(anchor, "hunk_not_found", [])
        return self._unresolved(anchor, "code_not_found", [])

    def _placement(self, anchor: EvidenceAnchor) -> tuple[CommentPlacement, str | None]:
        if anchor.resolution_method != EvidenceResolutionMethod.unresolved:
            candidate = anchor.candidate_locations[0]
            if self.diff_index.is_commentable(candidate):
                return CommentPlacement.inline, None
            return CommentPlacement.summary, "resolved_outside_diff"
        if anchor.unresolved_reason in {
            "multiple_matches", "side_conflict", "rename_ambiguity", "deletion_ambiguity"
        }:
            return CommentPlacement.needs_human, anchor.unresolved_reason
        return CommentPlacement.suppressed, anchor.unresolved_reason

    @staticmethod
    def _route_invalid(issue: ReviewIssue, reason: str) -> ReviewIssue:
        primary = issue.primary_evidence.model_copy(update={"unresolved_reason": reason})
        return issue.model_copy(update={
            "primary_evidence": primary,
            "placement": CommentPlacement.suppressed,
            "status": IssueStatus.dismissed,
            "unresolved_reason": reason,
            "auto_fix_eligible": False,
        })

    def _search_hunks(
        self,
        anchor: EvidenceAnchor,
        sides: Iterable[str],
        hunks: Iterable,
        normalized: bool,
    ) -> list[EvidenceCandidate]:
        candidates: list[EvidenceCandidate] = []
        for hunk in hunks:
            for side in sides:
                lines = self.diff_index.side_lines(hunk, side)
                candidates.extend(self._search_lines(
                    anchor, side, lines, normalized, hunk.hunk_id or None
                ))
        return self._deduplicate(candidates)

    def _search_files(
        self, anchor: EvidenceAnchor, sides: Iterable[str], normalized: bool
    ) -> list[EvidenceCandidate]:
        candidates: list[EvidenceCandidate] = []
        for side in sides:
            canonical = self.diff_index.canonical_path(anchor.file_path, side) or anchor.file_path
            content = self.file_loader(canonical, side)
            if content is None or "\x00" in content:
                continue
            raw_lines = content.split("\n")
            if raw_lines and raw_lines[-1] == "":
                raw_lines.pop()
            lines = [IndexedLine(value, index + 1) for index, value in enumerate(raw_lines)]
            candidates.extend(self._search_lines(anchor, side, lines, normalized, None))
        return self._deduplicate(candidates)

    def _symbol_candidates(
        self,
        anchor: EvidenceAnchor,
        sides: list[str],
        file_candidates: list[EvidenceCandidate],
    ) -> list[EvidenceCandidate]:
        ranges = [
            (int(item.get("start_line", 0)), int(item.get("end_line", 0)))
            for item in self.symbol_index
            if item.get("symbol") == anchor.symbol
            and item.get("file") in {
                anchor.file_path,
                self.diff_index.canonical_path(anchor.file_path, "head"),
            }
        ]
        narrowed = [
            candidate for candidate in file_candidates
            if candidate.side == "head"
            and any(start <= candidate.start_line and candidate.end_line <= end for start, end in ranges)
        ]
        if narrowed:
            return self._deduplicate(narrowed)
        candidates: list[EvidenceCandidate] = []
        for side in sides:
            if side != "head":
                continue
            canonical = self.diff_index.canonical_path(anchor.file_path, side) or anchor.file_path
            content = self.file_loader(canonical, side)
            if content is None:
                continue
            raw_lines = content.split("\n")
            for start, end in ranges:
                scoped = [
                    IndexedLine(raw_lines[index - 1], index)
                    for index in range(max(1, start), min(len(raw_lines), end) + 1)
                ]
                candidates.extend(self._search_lines(anchor, side, scoped, False, None))
                candidates.extend(self._search_lines(anchor, side, scoped, True, None))
        return self._deduplicate(candidates)

    @classmethod
    def _search_lines(
        cls,
        anchor: EvidenceAnchor,
        side: str,
        lines: list[IndexedLine],
        normalized: bool,
        hunk_id: str | None,
    ) -> list[EvidenceCandidate]:
        needle = cls._split_code(anchor.existing_code)
        compare = cls._normalize_line if normalized else (lambda value: value)
        expected = [compare(value) for value in needle]
        if not expected:
            return []
        candidates: list[EvidenceCandidate] = []
        width = len(expected)
        for index in range(0, len(lines) - width + 1):
            window = lines[index:index + width]
            if [compare(item.content) for item in window] != expected:
                continue
            if not cls._context_matches(anchor, lines, index, width, compare):
                continue
            candidates.append(EvidenceCandidate(
                file_path=anchor.file_path,
                side=side,
                start_line=window[0].line_no,
                end_line=window[-1].line_no,
                hunk_id=hunk_id,
            ))
        return candidates

    @classmethod
    def _context_matches(cls, anchor, lines, index, width, compare) -> bool:
        before = [compare(value) for value in anchor.context_before]
        after = [compare(value) for value in anchor.context_after]
        if before:
            actual = [compare(item.content) for item in lines[max(0, index - len(before)):index]]
            if actual != before[-len(actual):] or len(actual) != len(before):
                return False
        if after:
            actual = [compare(item.content) for item in lines[index + width:index + width + len(after)]]
            if actual != after:
                return False
        return True

    @staticmethod
    def _split_code(value: str) -> list[str]:
        lines = value.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        return lines

    @staticmethod
    def _normalize_line(value: str) -> str:
        return value.rstrip("\r").strip()

    @classmethod
    def _normalize_code(cls, value: str) -> str:
        return "\n".join(cls._normalize_line(line) for line in cls._split_code(value))

    @staticmethod
    def _deduplicate(candidates: list[EvidenceCandidate]) -> list[EvidenceCandidate]:
        unique: dict[tuple, EvidenceCandidate] = {}
        for item in candidates:
            key = (item.file_path, item.side, item.start_line, item.end_line, item.hunk_id)
            unique[key] = item
        return sorted(
            unique.values(),
            key=lambda item: (item.side, item.start_line, item.end_line, item.hunk_id or ""),
        )

    def _finish_candidates(
        self,
        anchor: EvidenceAnchor,
        method: EvidenceResolutionMethod,
        candidates: list[EvidenceCandidate],
    ) -> EvidenceAnchor:
        candidates = self._deduplicate(candidates)
        if len(candidates) != 1:
            return self._unresolved(anchor, "multiple_matches", candidates)
        candidate = candidates[0]
        surrounding = self._actual_surrounding(anchor, candidate)
        payload = {
            "file_path": anchor.file_path,
            "side": candidate.side,
            "existing_code": "\n".join(
                self._normalize_line(value) for value in self._split_code(anchor.existing_code)
            ),
            "resolved_range": [candidate.start_line, candidate.end_line],
            "surrounding_context": surrounding,
        }
        anchor_hash = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return anchor.model_copy(update={
            "resolved_start_line": candidate.start_line,
            "resolved_end_line": candidate.end_line,
            "resolution_method": method,
            "match_count": 1,
            "anchor_hash": anchor_hash,
            "resolved_side": candidate.side,
            "candidate_locations": candidates,
            "unresolved_reason": None,
        })

    def _actual_surrounding(
        self, anchor: EvidenceAnchor, candidate: EvidenceCandidate
    ) -> dict[str, list[str]]:
        canonical = (
            self.diff_index.canonical_path(anchor.file_path, candidate.side) or anchor.file_path
        )
        content = self.file_loader(canonical, candidate.side)
        if content is not None:
            lines = content.splitlines()
            start = candidate.start_line - 1
            end = candidate.end_line
            return {
                "before": [self._normalize_line(value) for value in lines[max(0, start - 3):start]],
                "after": [self._normalize_line(value) for value in lines[end:end + 3]],
            }
        if candidate.hunk_id:
            hunk = self.diff_index.hunk(anchor.file_path, candidate.hunk_id)
            if hunk is not None:
                lines = self.diff_index.side_lines(hunk, candidate.side)
                index = next(
                    (offset for offset, line in enumerate(lines) if line.line_no == candidate.start_line),
                    0,
                )
                width = candidate.end_line - candidate.start_line + 1
                return {
                    "before": [
                        self._normalize_line(line.content) for line in lines[max(0, index - 3):index]
                    ],
                    "after": [
                        self._normalize_line(line.content)
                        for line in lines[index + width:index + width + 3]
                    ],
                }
        return {"before": [], "after": []}

    @staticmethod
    def _unresolved(
        anchor: EvidenceAnchor,
        reason: str,
        candidates: list[EvidenceCandidate],
    ) -> EvidenceAnchor:
        return anchor.model_copy(update={
            "resolved_start_line": None,
            "resolved_end_line": None,
            "resolution_method": EvidenceResolutionMethod.unresolved,
            "match_count": len(candidates),
            "anchor_hash": None,
            "resolved_side": None,
            "candidate_locations": candidates,
            "unresolved_reason": reason,
        })

    @staticmethod
    def _is_binary(file_path: str, item) -> bool:
        return bool(item and item.is_binary) or PurePosixPath(file_path).suffix.casefold() in _BINARY_EXTENSIONS
