"""Diff 解析器 —— 将 unified diff 文本解析为结构化 ChangedFile 列表。"""

import hashlib
from io import StringIO

from unidiff import PatchSet

from app.models.review import ChangedFile, ChangedLine, DiffHunk, DiffLine


def stable_hunk_id(
    file_path: str,
    old_start: int,
    old_length: int,
    new_start: int,
    new_length: int,
    lines: list[DiffLine] | list[dict],
) -> str:
    """按路径、双侧范围和归一化 hunk 内容生成稳定 ID。"""
    normalized: list[str] = []
    for item in lines:
        kind = item.kind if isinstance(item, DiffLine) else str(item.get("kind", ""))
        content = item.content if isinstance(item, DiffLine) else str(item.get("content", ""))
        normalized.append(f"{kind}:{content.rstrip(chr(13)).strip()}")
    content_hash = hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()
    payload = (
        f"{file_path}\n{old_start},{old_length}\n{new_start},{new_length}\n{content_hash}"
    )
    return f"hunk-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"


class DiffParser:
    """使用 unidiff 库解析 git diff 输出，按文件/hunk/行三层组织。"""

    def parse(self, diff_text: str) -> list[ChangedFile]:
        """解析 unified diff 文本，返回 ChangedFile 列表。"""
        patch = PatchSet(StringIO(diff_text))
        files: list[ChangedFile] = []

        for patched_file in patch:
            hunks: list[DiffHunk] = []
            additions = 0
            deletions = 0

            for hunk in patched_file:
                added_lines: list[ChangedLine] = []
                removed_lines: list[ChangedLine] = []
                ordered_lines: list[DiffLine] = []
                for line in hunk:
                    content = line.value.rstrip("\n")
                    if line.is_added:
                        additions += 1
                        added_lines.append(
                            ChangedLine(line_no=line.target_line_no, content=content)
                        )
                        ordered_lines.append(DiffLine(
                            kind="added",
                            content=content,
                            new_line_no=line.target_line_no,
                        ))
                    elif line.is_removed:
                        deletions += 1
                        removed_lines.append(
                            ChangedLine(line_no=line.source_line_no, content=content)
                        )
                        ordered_lines.append(DiffLine(
                            kind="deleted",
                            content=content,
                            old_line_no=line.source_line_no,
                        ))
                    elif line.is_context:
                        ordered_lines.append(DiffLine(
                            kind="context",
                            content=content,
                            old_line_no=line.source_line_no,
                            new_line_no=line.target_line_no,
                        ))

                hunk_id = stable_hunk_id(
                    patched_file.path,
                    hunk.source_start,
                    hunk.source_length,
                    hunk.target_start,
                    hunk.target_length,
                    ordered_lines,
                )
                hunks.append(
                    DiffHunk(
                        old_start=hunk.source_start,
                        old_length=hunk.source_length,
                        new_start=hunk.target_start,
                        new_length=hunk.target_length,
                        hunk_id=hunk_id,
                        lines=ordered_lines,
                        added_lines=added_lines,
                        removed_lines=removed_lines,
                    )
                )

            source_path = patched_file.source_file.removeprefix("a/")
            target_path = patched_file.target_file.removeprefix("b/")
            if patched_file.is_added_file:
                change_type = "added"
            elif patched_file.is_removed_file:
                change_type = "deleted"
            elif source_path != target_path:
                change_type = "renamed"
            else:
                change_type = "modified"

            files.append(
                ChangedFile(
                    file_path=patched_file.path,
                    old_file_path=(
                        source_path
                        if source_path not in {patched_file.path, "/dev/null"}
                        else None
                    ),
                    change_type=change_type,
                    additions=additions,
                    deletions=deletions,
                    is_binary=bool(getattr(patched_file, "is_binary_file", False)),
                    hunks=hunks,
                )
            )

        return files
