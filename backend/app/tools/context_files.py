"""Review Unit 范围内的语言无关文件上下文工具。"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import json
from pathlib import Path
import secrets
from typing import Any

from app.models.review import ChangedFile, ReviewToolScope
from app.core.fingerprints import stable_hash
from app.services.review.review_input_context import build_scope_projection
from app.review.tool_scope import (
    ReviewPathPolicyError,
    is_sensitive_repository_path,
    is_sensitive_repository_change,
    list_git_tracked_files,
    validate_repository_file,
)

_MAX_CONTENT_CHARS = 20_000
_MAX_FIND_PAGE_CHARS = 6_000
_FILE_FIND_CURSORS: OrderedDict[str, tuple[str, str, str, int]] = OrderedDict()


def _file_find_cursor(binding: tuple[str, str, str, int]) -> str:
    for token, saved in list(_FILE_FIND_CURSORS.items()):
        if saved == binding:
            _FILE_FIND_CURSORS.move_to_end(token)
            return token
    token = secrets.token_urlsafe(24)
    _FILE_FIND_CURSORS[token] = binding
    if len(_FILE_FIND_CURSORS) > 1024:
        _FILE_FIND_CURSORS.popitem(last=False)
    return token


class ScopedContextToolError(ValueError):
    """上下文工具请求超出 Review Unit 的不可扩张范围。"""


class ScopedContextTool:
    """在安全 tracked repository 中发现文件，并对内容读取实施硬限制。"""

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
                requested_end,
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
        """保留旧的首屏列表接口；Agent 使用带分页元数据的 file_find_page。"""
        return (await self.file_find_page(scope=scope, query=query, max_results=max_results))["files"]

    async def file_find_page(
        self, *, scope: ReviewToolScope, query: str, max_results: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        normalized = query.strip().casefold()
        if not normalized or len(normalized) > 120 or any(ord(char) < 32 for char in normalized):
            raise ScopedContextToolError("file_find query must be a short literal")
        if "\\" in normalized or normalized.startswith(("/", "~")) or ".." in normalized.split("/"):
            raise ScopedContextToolError("file_find query must not contain path traversal")
        limit = min(max_results or scope.max_search_results, scope.max_search_results, 20)
        if limit < 1:
            raise ScopedContextToolError("file_find max_results must be positive")
        candidates = scope.readable_files if scope.repository_discovery_enabled else scope.seed_files
        matched = [
            path for path in sorted(candidates)
            if normalized in path.casefold() and not is_sensitive_repository_path(path)
        ]
        tracked = (await asyncio.to_thread(list_git_tracked_files, scope.repository_root)
                   if scope.repository_root else None)
        scope_id = build_scope_projection(scope)["id"]
        version = stable_hash([matched, sorted(tracked) if tracked is not None else None])
        offset = 0
        if cursor is not None:
            saved = _FILE_FIND_CURSORS.get(cursor)
            if saved is None or saved[:3] != (scope_id, normalized, version):
                raise ScopedContextToolError("file_find_cursor_invalid_or_expired: restart the query without a cursor")
            _FILE_FIND_CURSORS.move_to_end(cursor)
            offset = saved[3]
        safe: list[str] = []
        # 预留最大计数与 24-byte cursor 的 JSON 包装，约束整个返回对象而非仅路径正文。
        page = {"files": safe, "scope_id": scope_id, "matched_candidates": len(matched),
                "search_scope": "readable_files" if scope.repository_discovery_enabled else "seed_files",
                "absence_is_repository_absence": False,
                "scanned_candidates": 100, "omitted_for_safety": 100, "next_cursor": "x" * 32}
        path_budget = _MAX_FIND_PAGE_CHARS - (len(json.dumps(page, ensure_ascii=False)) - 2)
        index, scanned, omitted, size = offset, 0, 0, 2
        while index < len(matched) and len(safe) < limit and scanned < 100:
            path = matched[index]
            path_size = len(json.dumps(path, ensure_ascii=False)) + (2 if safe else 0)
            if size + path_size > path_budget:
                if not safe:
                    raise ScopedContextToolError("file_find_path_exceeds_page_budget")
                break
            index += 1
            scanned += 1
            if scope.repository_root:
                try:
                    await asyncio.to_thread(validate_repository_file, scope.repository_root, path, tracked_files=tracked)
                except (OSError, ReviewPathPolicyError):
                    omitted += 1
                    continue
            safe.append(path)
            size += path_size
        page.update(scanned_candidates=scanned, omitted_for_safety=omitted,
            next_cursor=_file_find_cursor((scope_id, normalized, version, index)) if index < len(matched) else None)
        return page

    @staticmethod
    def normalize_diff_hunk_ids(target, hunk_ids):
        """只转换当前授权文件正文可重建的原始 ID 与 manifest ID。"""
        from app.services.review.review_planner import DeterministicReviewPlanner
        from app.core.fingerprints import stable_hash

        aliases = {}
        for index, hunk in enumerate(target.hunks):
            identity = DeterministicReviewPlanner.hunk_id(target.file_path, index, hunk.model_dump(mode="json"))
            for alias in {identity, hunk.hunk_id, "hunk-" + stable_hash([target.file_path, identity])[:24]} - {None}:
                aliases.setdefault(alias, set()).add(identity)
        unknown = set(hunk_ids or ()) - set(aliases)
        ambiguous = [item for item in hunk_ids or () if len(aliases.get(item, ())) > 1]
        if unknown or ambiguous:
            raise ScopedContextToolError("file_read_diff references an unknown hunk_id; " + json.dumps({
                "file_path": target.file_path, "unknown_hunk_ids": sorted(unknown),
                "ambiguous_hunk_ids": ambiguous,
                "accepted_hunk_ids": sorted(key for key, values in aliases.items() if len(values) == 1)}, ensure_ascii=False))
        return list(dict.fromkeys(next(iter(aliases[item])) for item in hunk_ids or ()))

    async def file_read_diff(
        self,
        *,
        scope: ReviewToolScope,
        file_path: str,
        changed_files: list[ChangedFile | dict[str, Any]],
        hunk_ids: list[str] | None = None,
        supporting_files: list[ChangedFile | dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """读取当前批次可评论 Diff；或父 Unit 同一清单内的支持性 Diff。

        支持性读取不授予评论权限、目标覆盖或引用权限：是否可引用仍由证据目录决定。
        """
        self._require_readable(scope, file_path)
        current = [
            item if isinstance(item, ChangedFile) else ChangedFile.model_validate(item)
            for item in changed_files
        ]
        target = next((item for item in current if item.file_path == file_path), None)
        if target is not None and file_path not in scope.commentable_files:
            target = None  # 当前批次正文仅在评论范围内可读
        supporting = False
        if target is None and file_path not in scope.commentable_files:
            support = [
                item if isinstance(item, ChangedFile) else ChangedFile.model_validate(item)
                for item in supporting_files or []
            ]
            target = next((item for item in support if item.file_path == file_path), None)
            supporting = target is not None
        if target is None:
            raise ScopedContextToolError(
                "file_read_diff accepts changed files in the current Unit only"
                if file_path in scope.commentable_files else
                "file_read_diff accepts commentable changed files in the current Unit only"
                if supporting_files is None else
                "file_read_diff accepts current Unit commentable files or same-manifest "
                "supporting diffs only"
            )
        if is_sensitive_repository_change(target.file_path, target.old_file_path):
            raise ScopedContextToolError(
                "sensitive repository change is not readable: "
                f"{target.old_file_path or target.file_path} -> {target.file_path}"
            )
        selected = set(self.normalize_diff_hunk_ids(target, hunk_ids))
        from app.services.review.review_planner import DeterministicReviewPlanner

        rendered: list[str] = [f"--- a/{target.old_file_path or file_path}", f"+++ b/{file_path}"]
        included = 0
        selected_hunks = []
        for index, hunk in enumerate(target.hunks):
            identity = DeterministicReviewPlanner.hunk_id(file_path, index, hunk.model_dump(mode="json"))
            if selected and identity not in selected:
                continue
            selected_hunks.append(hunk)
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
        start = min((hunk.new_start for hunk in selected_hunks), default=1)
        end = max((hunk.new_start + max(hunk.new_length - 1, 0) for hunk in selected_hunks), default=start)
        content = self._truncate_content("\n".join(rendered))
        return {
            "file": file_path,
            "start_line": start,
            "end_line": end,
            "content": content,
            "relevance": "contract" if supporting else "direct",
            "review_unit_id": scope.review_unit_id,
            "source": "file_read_diff",
            "distance": 0,
            "confidence": 1.0,
            "why_retrieved": (
                f"读取父 Unit 同一清单内的 {included} 个支持性 diff hunk"
                if supporting else f"读取当前 Review Unit 的 {included} 个 diff hunk"),
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
        requested_end_line: int | None = None,
    ) -> dict[str, Any]:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if start_line > len(lines) and lines:
            raise ScopedContextToolError("start_line exceeds file length")
        actual_end = min(end_line, max(len(lines), 1))
        content = "\n".join(lines[start_line - 1:actual_end])
        content = ScopedContextTool._truncate_content(content)
        actual_end = start_line + max(0, len(content.splitlines()) - 1)
        return {
            "file": file_path,
            "start_line": start_line,
            "end_line": actual_end,
            "content": content,
            "requested_end_line": requested_end_line or end_line,
            "total_lines": len(lines),
            "truncated": actual_end < min(requested_end_line or end_line, len(lines)),
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
        from app.services.review.review_input_context import complete_line_prefix

        return complete_line_prefix(content, _MAX_CONTENT_CHARS)
