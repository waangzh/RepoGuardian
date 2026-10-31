"""Bounded changed-file relations for plan-time grouping; never model evidence."""

import logging
import time
from pathlib import Path, PurePosixPath

from app.languages.isolation import ParserCancelled, analyze_isolated
from app.languages.registry import default_language_registry
from app.review.planner import excluded_changes
from app.review.tool_scope import list_git_tracked_blobs, validate_repository_file
from app.tools.repository_graph import related_test_edges, resolved_import_edges

logger = logging.getLogger("RepoGuardian.Review.Relations")

MAX_RELATION_FILES = 128
MAX_RELATION_BYTES = 4 * 1024 * 1024
MAX_RELATION_FILE_BYTES = 512 * 1024
# shortcut: the wall-clock cap can make pathological PRs group partially;
# upgrade to a deterministic per-file budget if plan reproducibility requires it.
RELATION_DEADLINE_SECONDS = 10.0


class _RelationCancellation:
    """Use the parser's cancellation path for both caller and plan deadlines."""

    def __init__(self, cancel_event, deadline):
        self.cancel_event, self.deadline = cancel_event, deadline

    def is_set(self):
        return ((self.cancel_event is not None and self.cancel_event.is_set())
                or time.monotonic() >= self.deadline)


def change_relations(repo_path, changed_files, options, *, cancel_event=None):
    """Return (priority, path, path) edges among included changed files.

    Parsing is bounded and fail-open: an unreadable, oversized or unparsable
    file simply contributes no import edge. Candidate imports, unresolved calls
    and stem fallbacks are deliberately excluded from grouping.
    """
    excluded = excluded_changes(changed_files, options)
    included = sorted(change.file_path for change in changed_files
                      if change.file_path not in excluded)
    by_path = {change.file_path: change for change in changed_files}
    entries = {path: {"path": path,
        "language": default_language_registry.detect_language(path),
        "imports": [], "import_refs": []} for path in included}
    aliases = {}
    for path in included:
        old = by_path[path].old_file_path
        if old and old != path and old not in included and old not in excluded:
            entries.setdefault(old, {"path": old,
                "language": default_language_registry.detect_language(old),
                "imports": [], "import_refs": []})
            aliases[old] = path
    root = Path(repo_path).resolve(strict=True)
    tracked_blobs = list_git_tracked_blobs(root)
    tracked = set(tracked_blobs)
    is_git = (root / ".git").exists()
    parsed, total_bytes, skipped = set(), 0, 0
    deadline = time.monotonic() + RELATION_DEADLINE_SECONDS
    cancellation = _RelationCancellation(cancel_event, deadline)
    timed_out = False
    for path in included:
        if cancellation.is_set():
            timed_out = time.monotonic() >= deadline
            break
        if len(parsed) >= MAX_RELATION_FILES or total_bytes >= MAX_RELATION_BYTES:
            break
        adapter = default_language_registry.for_path(path)
        if adapter is None:
            continue
        try:
            if is_git and not tracked_blobs.get(path):
                raise ValueError("relation_source_not_regular_tracked_blob")
            candidate = validate_repository_file(root, path, tracked_files=tracked)
            unresolved = root.joinpath(*PurePosixPath(path).parts)
            for component in (unresolved, *unresolved.parents):
                if component == root:
                    break
                if (component.is_symlink()
                        or getattr(component, "is_junction", lambda: False)()):
                    raise ValueError("relation_source_link")
            size = candidate.stat().st_size
            if size > MAX_RELATION_FILE_BYTES or total_bytes + size > MAX_RELATION_BYTES:
                skipped += 1
                continue
            source = candidate.read_bytes()
        except (OSError, ValueError):
            skipped += 1
            continue
        if len(source) > MAX_RELATION_FILE_BYTES:
            skipped += 1
            continue
        total_bytes += len(source)
        try:
            analysis = analyze_isolated(adapter, source, path,
                PurePosixPath(path).suffix.casefold(), cancel_event=cancellation)
        except ParserCancelled:
            timed_out = time.monotonic() >= deadline
            break
        except Exception:
            skipped += 1
            continue
        entries[path]["imports"] = sorted({ref.module for ref in analysis.imports})
        entries[path]["import_refs"] = [ref.model_dump(mode="json") for ref in analysis.imports]
        parsed.add(path)
    edges = set()
    for source, target, _ in resolved_import_edges(parsed, entries):
        target = aliases.get(target, target)
        if target in included and target != source:
            edges.add((0, *sorted((source, target))))
    for test, source, _ in related_test_edges(included, entries):
        edges.add((1, *sorted((test, source))))
    by_directory = {}
    for path in included:
        by_directory.setdefault(PurePosixPath(path).parent.as_posix(), []).append(path)
    for directory in sorted(by_directory):
        paths = sorted(by_directory[directory])
        if len(paths) == 2:
            edges.add((2, paths[0], paths[1]))
    logger.info("关系分析完成 changed=%d parsed=%d edges=%d skipped=%d timed_out=%s",
                len(included), len(parsed), len(edges), skipped, timed_out)
    return sorted(edges)
