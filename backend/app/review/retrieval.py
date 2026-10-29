"""Bounded pagination over authorized immutable sources and the fixed diff."""

import asyncio
import fnmatch
import json
import re
import subprocess

from app.review.evidence import digest, source_lines
from app.review.tool_scope import (
    _validate_repository_relative_path, is_sensitive_repository_path, validate_repository_file,
)

PAGE_CHARS = 16_000
SCAN_FILES = 4096


def _size(value):
    return len(json.dumps(value))


def _limit(value, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError("invalid_page_size")


def _scope(context, operation, parameters):
    snapshot = context.evidence.snapshot.model_dump()
    return digest(json.dumps([snapshot, operation, parameters,
        sorted(context.evidence.allowed_paths - context.denied_paths)], sort_keys=True))


def _position(context, cursor, scope, size):
    if cursor == "":
        return 0, 0, 0
    try:
        if not re.fullmatch(r"[0-9a-f]{64}", cursor):
            raise ValueError("invalid_cursor")
        service = context.evidence
        artifact = service.artifacts.root / service.snapshot.review_id / f"cursor-{cursor}.txt"
        encoded = service.artifacts.read_text(artifact.as_uri())
        if digest(encoded) != cursor:
            raise ValueError("invalid_cursor")
        value = json.loads(encoded)
        position, column, unreadable = value["position"], value["column"], value["unreadable_files"]
        if (value["scope"] != scope or type(position) is not int or type(column) is not int
                or not 0 <= position < size or column < 0
                or type(unreadable) is not int or unreadable < 0):
            raise ValueError("invalid_cursor")
        if (position, column) == (0, 0):
            raise ValueError("invalid_cursor")
        return position, column, unreadable
    except (ValueError, KeyError, TypeError, UnicodeError, OSError) as exc:
        raise ValueError("invalid_cursor") from exc


def _page(context, scope, cursor, position, has_more, *, unreadable_total=0, search_window=None, **body):
    next_cursor = None
    if has_more:
        artifact = context.evidence.artifacts.put_text(
            task_id=context.evidence.snapshot.review_id, kind="cursor",
            content=json.dumps({"scope": scope, "position": position[0], "column": position[1],
                                "unreadable_files": unreadable_total, "search_window": search_window},
                               sort_keys=True))
        next_cursor = artifact.sha256
    return {"ok": True, **body, "pagination_id": scope, "cursor": cursor,
            "next_cursor": next_cursor, "has_more": has_more, "truncated": False}


def _paths(context, path="", glob=""):
    if path:
        _validate_repository_relative_path(path.rstrip("/"))
        if path.rstrip("/") in context.denied_paths or is_sensitive_repository_path(path.rstrip("/")):
            raise ValueError("path_not_authorized")
    if len(glob) > 200 or "\\" in glob or "\x00" in glob or ".." in glob.split("/"):
        raise ValueError("invalid_path_filter")
    prefix = path.rstrip("/")
    return sorted(item for item in context.evidence.allowed_paths
        if item not in context.denied_paths and not is_sensitive_repository_path(item)
        and (not prefix or item == prefix or item.startswith(prefix + "/"))
        and (not glob or fnmatch.fnmatchcase(item, glob)))


def _matching_paths(service, paths, side, query):
    sha = service.snapshot.head_sha if side == "head" else service.snapshot.base_sha
    entries = service.git.get_tree_entries(service.repo_path, sha, paths)
    readable, unreadable, excluded, remaining_bytes = [], set(), set(), 8 * 1024 * 1024
    processed = 0
    for path in paths:
        entry = entries.get(path)
        if entry is None:
            processed += 1
            continue  # A BASE deletion or old rename path need not exist in HEAD.
        try:
            if entry.get("mode") not in {"100644", "100755"}:
                excluded.add(path)
                processed += 1
                continue
            if entry.get("size") is None or entry["size"] > service.max_file_bytes:
                raise ValueError("source_too_large")
            if entry["size"] > remaining_bytes:
                break
            local = service.repo_path / path
            if local.exists() or local.is_symlink():
                validate_repository_file(service.repo_path, path, require_tracked_if_git=False)
            readable.append(path)
            remaining_bytes -= entry["size"]
        except (ValueError, OSError):
            unreadable.add(path)
        processed += 1
    return service.git.find_matching_paths(service.repo_path, sha, query, readable), unreadable, excluded, processed


async def search(context, query, path="", glob="", side="head", cursor="", limit=12):
    _limit(limit, 40)
    if not query or len(query) > 200 or "\x00" in query or "\n" in query or side not in {"head", "base"}:
        raise ValueError("invalid_search")
    paths = _paths(context, path, glob)
    scope = _scope(context, "search", [query, path, glob, side])
    first, line_start, prior_unreadable = await asyncio.to_thread(
        _position, context, cursor, scope, len(paths))
    window = None
    if cursor:
        service = context.evidence
        artifact = service.artifacts.root / service.snapshot.review_id / f"cursor-{cursor}.txt"
        encoded = await asyncio.to_thread(service.artifacts.read_text, artifact.as_uri())
        if digest(encoded) != cursor:
            raise ValueError("invalid_cursor")
        saved = json.loads(encoded)
        window = saved.get("search_window")
    stop = min(len(paths), first + SCAN_FILES)
    matches, matched, unreadable, scanned = [], 0, 0, 0
    # git grep only locates matching files; source reads still enforce blob and path policy.
    try:
        if window:
            stop = window["stop"]
            hits, rejected, excluded = set(window["hits"]), set(), set()
            if not first < stop <= len(paths) or not hits <= set(paths[:stop]):
                raise ValueError("invalid_cursor")
        else:
            hits, rejected, excluded, processed = await asyncio.to_thread(
                _matching_paths, context.evidence, paths[first:stop], side, query)
            scanned = processed
            stop = first + processed
            window = {"stop": stop, "hits": sorted(hits)}
    except subprocess.TimeoutExpired as exc:
        raise ValueError("snapshot_search_timeout") from exc
    # Locate matches across this bounded scan before paging source hits, not unrelated paths.
    unreadable = len(rejected)
    positions = {selected: index for index, selected in enumerate(paths)}
    for selected in sorted(hits, key=positions.__getitem__):
        index = positions[selected]
        if index < first:
            continue
        if context.check_active:
            await context.check_active()
        matched += 1
        try:
            _, content = await asyncio.to_thread(context.evidence._source, selected, side)
        except (ValueError, OSError):
            unreadable += 1
            continue
        lines = source_lines(content)
        begin = line_start if index == first else 0
        if begin > len(lines):
            raise ValueError("invalid_cursor")
        for line in range(begin, len(lines)):
            if query not in lines[line]:
                continue
            match = {"path": selected, "line": line + 1, "text": lines[line][:300], "side": side}
            if len(matches) >= limit or _size(matches + [match]) > PAGE_CHARS:
                return await asyncio.to_thread(
                    _page, context, scope, cursor, (index, line), True, source="search_hint",
                    matches=matches, scanned_files=scanned, matched_files=matched, unreadable_files=unreadable,
                    policy_exclusions=len(excluded),
                    search_window=window,
                    unreadable_total=prior_unreadable + unreadable,
                    scope_files=len(paths), scope_complete=False)
            matches.append(match)
    return await asyncio.to_thread(
        _page, context, scope, cursor, (stop, 0), stop < len(paths), source="search_hint",
        matches=matches, scanned_files=scanned, matched_files=matched, unreadable_files=unreadable,
        policy_exclusions=len(excluded),
        unreadable_total=prior_unreadable + unreadable,
        scope_files=len(paths), scope_complete=stop == len(paths) and not (prior_unreadable + unreadable))


def find_paths(context, query="", path="", glob="", cursor="", limit=80):
    _limit(limit, 80)
    if len(query) > 200 or "\x00" in query:
        raise ValueError("invalid_path_query")
    paths = [item for item in _paths(context, path, glob) if query in item]
    scope = _scope(context, "paths", [query, path, glob])
    index, column, _ = _position(context, cursor, scope, len(paths))
    if column:
        raise ValueError("invalid_cursor")
    selected = []
    while index < len(paths) and len(selected) < limit:
        if _size(selected + [paths[index]]) > PAGE_CHARS:
            if not selected:
                raise ValueError("path_too_large")
            break
        selected.append(paths[index])
        index += 1
    return _page(context, scope, cursor, (index, 0), index < len(paths),
                 source="path_hint", paths=selected, scope_files=len(paths))


def diff_page(context, path, hunk_index=None, start=0, cursor="", limit=60):
    _limit(limit, 80)
    if type(start) is not int or start < 0:
        raise ValueError("invalid_diff_range")
    if path not in _paths(context, path):
        raise ValueError("path_not_authorized")
    change = next((item for item in context.changed_files if item["file_path"] == path), None)
    if change is None or (change.get("old_file_path") and
                          is_sensitive_repository_path(change["old_file_path"])):
        raise ValueError("diff_not_authorized")
    hunks = change.get("hunks", [])
    scope = _scope(context, "diff", [digest(json.dumps(change, sort_keys=True)), hunk_index, start])
    metadata = {key: value for key, value in change.items() if key != "hunks"}
    if hunk_index is None:
        index, column, _ = _position(context, cursor, scope, len(hunks))
        if column or start:
            raise ValueError("invalid_diff_range")
        selected = []
        while index < len(hunks) and len(selected) < limit:
            hunk = hunks[index]
            item = {key: value for key, value in hunk.items() if key != "lines"}
            item.update(hunk_index=index, line_count=len(hunk.get("lines", [])))
            if _size(selected + [item]) > PAGE_CHARS:
                if not selected:
                    raise ValueError("diff_metadata_too_large")
                break
            selected.append(item)
            index += 1
        return _page(context, scope, cursor, (index, 0), index < len(hunks),
                     source="fixed_diff", file=metadata, hunks=selected)
    if type(hunk_index) is not int or not 0 <= hunk_index < len(hunks):
        raise ValueError("invalid_hunk_index")
    lines = hunks[hunk_index].get("lines", [])
    index, column, _ = _position(context, cursor, scope, len(lines)) if cursor else (start, 0, 0)
    if index > len(lines) or (index == len(lines) and index != 0):
        raise ValueError("invalid_diff_range")
    selected = []
    while index < len(lines) and len(selected) < limit:
        line = lines[index]
        content = line.get("content", "")
        if column > len(content):
            raise ValueError("invalid_cursor")
        fragment = content[column:column + 1000]
        item = {**line, "content": fragment, "line_index": index,
                "content_offset": column, "continued": column + len(fragment) < len(content)}
        if _size(selected + [item]) > PAGE_CHARS:
            if not selected:
                raise ValueError("diff_line_metadata_too_large")
            break
        selected.append(item)
        if item["continued"]:
            column += len(fragment)
        else:
            index, column = index + 1, 0
    return _page(context, scope, cursor, (index, column), index < len(lines), source="fixed_diff",
                 file=metadata, hunk_index=hunk_index, lines=selected)
