"""统一 diff 的确定性双侧索引。"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.review import ChangedFile, DiffHunk, EvidenceCandidate


@dataclass(frozen=True)
class IndexedLine:
    content: str
    line_no: int


class DiffIndex:
    """按文件别名、side 和稳定 hunk ID 提供只读检索。"""

    def __init__(self, changed_files: list[ChangedFile] | list[dict]) -> None:
        self.files = [
            item if isinstance(item, ChangedFile) else ChangedFile.model_validate(item)
            for item in changed_files
        ]
        self._aliases: dict[str, ChangedFile] = {}
        self._hunks: dict[tuple[str, str], DiffHunk] = {}
        for item in self.files:
            self._aliases[item.file_path] = item
            if item.old_file_path:
                self._aliases[item.old_file_path] = item
            for hunk in item.hunks:
                if hunk.hunk_id:
                    self._hunks[(item.file_path, hunk.hunk_id)] = hunk

    def file_for_path(self, file_path: str) -> ChangedFile | None:
        return self._aliases.get(file_path)

    def canonical_path(self, file_path: str, side: str) -> str | None:
        item = self.file_for_path(file_path)
        if item is None:
            return None
        if side == "base":
            return item.old_file_path or item.file_path
        if item.change_type == "deleted":
            return None
        return item.file_path

    def hunk(self, file_path: str, hunk_id: str) -> DiffHunk | None:
        item = self.file_for_path(file_path)
        if item is None:
            return None
        return self._hunks.get((item.file_path, hunk_id))

    def hunks(self, file_path: str) -> list[DiffHunk]:
        item = self.file_for_path(file_path)
        return list(item.hunks) if item else []

    @staticmethod
    def side_lines(hunk: DiffHunk, side: str) -> list[IndexedLine]:
        result: list[IndexedLine] = []
        for line in hunk.lines:
            if side == "head" and line.kind != "deleted" and line.new_line_no is not None:
                result.append(IndexedLine(line.content, line.new_line_no))
            elif side == "base" and line.kind != "added" and line.old_line_no is not None:
                result.append(IndexedLine(line.content, line.old_line_no))
        return result

    def is_commentable(self, candidate: EvidenceCandidate) -> bool:
        item = self.file_for_path(candidate.file_path)
        if item is None:
            return False
        canonical = self.canonical_path(candidate.file_path, candidate.side)
        if canonical is None:
            return False
        commentable: set[int] = set()
        for hunk in item.hunks:
            commentable.update(line.line_no for line in self.side_lines(hunk, candidate.side))
        return all(
            line_no in commentable
            for line_no in range(candidate.start_line, candidate.end_line + 1)
        )
