"""Diff 解析器 —— 将 unified diff 文本解析为结构化 ChangedFile 列表。"""

from io import StringIO

from unidiff import PatchSet

from app.models.review import ChangedFile, ChangedLine, DiffHunk


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
                for line in hunk:
                    if line.is_added:
                        additions += 1
                        added_lines.append(
                            ChangedLine(line_no=line.target_line_no, content=line.value.rstrip("\n"))
                        )
                    elif line.is_removed:
                        deletions += 1
                        removed_lines.append(
                            ChangedLine(line_no=line.source_line_no, content=line.value.rstrip("\n"))
                        )

                hunks.append(
                    DiffHunk(
                        old_start=hunk.source_start,
                        old_length=hunk.source_length,
                        new_start=hunk.target_start,
                        new_length=hunk.target_length,
                        added_lines=added_lines,
                        removed_lines=removed_lines,
                    )
                )

            if patched_file.is_added_file:
                change_type = "added"
            elif patched_file.is_removed_file:
                change_type = "deleted"
            else:
                change_type = "modified"

            files.append(
                ChangedFile(
                    file_path=patched_file.path,
                    change_type=change_type,
                    additions=additions,
                    deletions=deletions,
                    hunks=hunks,
                )
            )

        return files

