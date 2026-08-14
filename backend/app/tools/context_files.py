"""Review Unit 范围内的语言无关文件上下文工具。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.models.review import ChangedFile, ReviewToolScope
from app.review.tool_scope import (
    ReviewPathPolicyError,
    is_sensitive_repository_path,
    validate_repository_file,
)

_MAX_CONTENT_CHARS = 20_000


class ScopedContextToolError(ValueError):
    """上下文工具请求超出 Review Unit 的不可扩张范围。"""


class ScopedContextTool:
    """只读取 Planner 已授权的文件或已解析 diff，不扫描范围外路径。"""

    async def file_read(
        self,
        *,
        scope: ReviewToolScope,
        file_path: str,
        start_line: int = 1,
        end_line: int | None = None,
    ) -> dict[str, Any]:
        self._require_readable(scope, file_path)
        if not scope.repository_root:
            raise ScopedContextToolError("repository_root is required for file_read")
        if start_line < 1:
            raise ScopedContextToolError("start_line must be positive")
        requested_end = end_line or start_line + scope.max_lines_per_read - 1
        if requested_end < start_line:
            raise ScopedContextToolError("end_line must not precede start_line")
        effective_end = min(requested_end, start_line + scope.max_lines_per_read - 1)
        try:
            path = validate_repository_file(scope.repository_root, file_path)
        except (OSError, ReviewPathPolicyError) as exc:
            raise ScopedContextToolError(str(exc)) from exc
        try:
            return await asyncio.to_thread(
                self._read_lines,
                path,
                file_path,
                start_line,
                effective_end,
                scope.review_unit_id,
                requested_end > effective_end,
            )
        except OSError as exc:
            raise ScopedContextToolError(f"unable to read repository file: {file_path}") from exc

    async def file_find(
        self,
        *,
        scope: ReviewToolScope,
        query: str,
        max_results: int | None = None,
    ) -> list[str]:
        normalized = query.strip().casefold()
        if not normalized or len(normalized) > 120 or any(ord(char) < 32 for char in normalized):
            raise ScopedContextToolError("file_find query must be a short literal")
        if "\\" in normalized or normalized.startswith(("/", "~")) or ".." in normalized.split("/"):
            raise ScopedContextToolError("file_find query must not contain path traversal")
        limit = min(max_results or scope.max_search_results, scope.max_search_results)
        return [
            path
            for path in sorted(scope.readable_files)
            if normalized in path.casefold() and not is_sensitive_repository_path(path)
        ][:limit]

    async def file_read_diff(
        self,
        *,
        scope: ReviewToolScope,
        file_path: str,
        changed_files: list[ChangedFile | dict[str, Any]],
        hunk_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        self._require_readable(scope, file_path)
        changed = [
            item if isinstance(item, ChangedFile) else ChangedFile.model_validate(item)
            for item in changed_files
        ]
        target = next((item for item in changed if item.file_path == file_path), None)
        if target is None:
            raise ScopedContextToolError("file_read_diff accepts changed files in the current Unit only")
        selected = set(hunk_ids or ())
        known = {hunk.hunk_id for hunk in target.hunks if hunk.hunk_id}
        if selected - known:
            raise ScopedContextToolError("file_read_diff references an unknown hunk_id")

        rendered: list[str] = [f"--- a/{target.old_file_path or file_path}", f"+++ b/{file_path}"]
        included = 0
        for hunk in target.hunks:
            if selected and hunk.hunk_id not in selected:
                continue
            included += 1
            rendered.append(
                f"@@ -{hunk.old_start},{hunk.old_length} +{hunk.new_start},{hunk.new_length} @@"
            )
            for line in hunk.lines:
                prefix = {"added": "+", "deleted": "-", "context": " "}[line.kind]
                rendered.append(prefix + line.content)
        truncated = len(rendered) > scope.max_lines_per_read
        rendered = rendered[:scope.max_lines_per_read]
        if truncated:
            rendered.append("...(truncated)")
        start = min((hunk.new_start for hunk in target.hunks), default=1)
        end = max((hunk.new_start + max(hunk.new_length - 1, 0) for hunk in target.hunks), default=start)
        content = self._truncate_content("\n".join(rendered))
        return {
            "file": file_path,
            "start_line": start,
            "end_line": end,
            "content": content,
            "relevance": "direct",
            "review_unit_id": scope.review_unit_id,
            "source": "file_read_diff",
            "distance": 0,
            "confidence": 1.0,
            "why_retrieved": f"读取当前 Review Unit 的 {included} 个 diff hunk",
        }

    @staticmethod
    def _require_readable(scope: ReviewToolScope, file_path: str) -> None:
        if file_path not in scope.readable_files:
            raise ScopedContextToolError(f"file is outside Review Unit readable scope: {file_path}")
        if is_sensitive_repository_path(file_path):
            raise ScopedContextToolError(f"sensitive repository path is not readable: {file_path}")

    @staticmethod
    def _read_lines(
        path: Path,
        file_path: str,
        start_line: int,
        end_line: int,
        review_unit_id: str,
        truncated_by_scope: bool,
    ) -> dict[str, Any]:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if start_line > len(lines) and lines:
            raise ScopedContextToolError("start_line exceeds file length")
        actual_end = min(end_line, max(len(lines), 1))
        content = "\n".join(lines[start_line - 1:actual_end])
        if truncated_by_scope or end_line < len(lines):
            content += "\n...(truncated)"
        return {
            "file": file_path,
            "start_line": start_line,
            "end_line": actual_end,
            "content": ScopedContextTool._truncate_content(content),
            "relevance": "direct",
            "review_unit_id": review_unit_id,
            "source": "file_read",
            "distance": 0,
            "confidence": 1.0,
            "why_retrieved": "按 Review Unit 明确授权的行范围读取文件",
        }

    @staticmethod
    def _truncate_content(content: str) -> str:
        if len(content) <= _MAX_CONTENT_CHARS:
            return content
        return content[:_MAX_CONTENT_CHARS].rstrip() + "\n...(truncated)"
