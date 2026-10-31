"""Deterministic relation-aware packing; relation discovery is a separate bounded step."""

import hashlib
import json
from functools import lru_cache

import tiktoken

from app.review.domain import PLAN_VERSION, Plan, Unit
from app.review.risk import _risk
from app.review.tool_scope import is_sensitive_repository_change

MAX_UNIT_PATHS = 10
HUNK_LINES = 120
MAX_UNIT_TOKENS = 200_000


@lru_cache(maxsize=1)
def _encoding():
    return tiktoken.get_encoding("cl100k_base")


def tokens(value) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return len(_encoding().encode(text, disallowed_special=())) + 128


def unit_capacity(limits) -> int:
    """Reserve prompt and response headroom while using the model context."""
    available = limits.context_tokens - limits.output_tokens - 1_024
    return max(1, min(MAX_UNIT_TOKENS, available // 4))


def identity(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def excluded_changes(changed_files, options) -> dict[str, str]:
    excluded = {}
    for change in changed_files:
        path = change.file_path
        if change.is_binary:
            excluded[path] = "binary"
        elif is_sensitive_repository_change(path, change.old_file_path):
            excluded[path] = "sensitive_path"
        elif options.paths is not None and path not in options.paths:
            excluded[path] = "user_excluded"
    return excluded


def members(changed_files):
    """Split large hunks at source-line boundaries; never omit a changed member.

    Iteration is sorted so the plan does not depend on Diff input order.
    """
    result = {}
    for change in sorted(changed_files, key=lambda item: (
            item.file_path, item.old_file_path or "", item.change_type)):
        path = change.file_path
        for hunk in sorted(change.hunks, key=lambda item: (
                item.new_start, item.old_start, item.hunk_id)):
            for start in range(0, len(hunk.lines), HUNK_LINES):
                lines = hunk.lines[start:start + HUNK_LINES]
                key = f"{hunk.hunk_id}:{start}"
                result[key] = {"id": key, "path": path, "old_path": change.old_file_path or path,
                    "lines": [line.model_dump() for line in lines]}
        if not change.hunks:
            key = f"structural-{identity(change.model_dump())[:20]}"
            result[key] = {"id": key, "path": path, "old_path": change.old_file_path or path,
                "change_type": change.change_type, "old_mode": change.old_mode,
                "new_mode": change.new_mode, "lines": []}
    return result


def _find(parent, path):
    root = path
    while parent[root] != root:
        root = parent[root]
    while parent[path] != path:
        parent[path], path = root, parent[path]
    return root


def _bundles(path_members, path_cost,
             relations: list[tuple[int, str, str]] | None, capacity):
    """Merge related paths into bounded components, then order them for packing."""
    parent = {path: path for path in path_members}
    groups = {path: {path} for path in path_members}
    costs = dict(path_cost)
    for _, left, right in sorted(relations or ()):
        if left not in parent or right not in parent or left == right:
            continue
        a, b = _find(parent, left), _find(parent, right)
        if a == b or len(groups[a] | groups[b]) > MAX_UNIT_PATHS or costs[a] + costs[b] > capacity:
            continue
        keep, drop = (a, b) if a < b else (b, a)
        parent[drop] = keep
        groups[keep] |= groups[drop]
        costs[keep] += costs[drop]
        del groups[drop], costs[drop]
    bundles = []
    for root in sorted(groups):
        paths = tuple(sorted(groups[root]))
        entries = [(key, member) for path in paths for key, member in path_members[path]]
        bundles.append((paths, entries, costs[root]))
    return sorted(bundles, key=lambda item: (-item[2], item[0]))

def plan_review(changed_files, snapshot, options,
                relations: list[tuple[int, str, str]] | None = None, *, file_groups=None) -> Plan:
    excluded = excluded_changes(changed_files, options)
    by_path = {change.file_path: change.model_dump() for change in changed_files}
    known = set(by_path) - {path for path, reason in excluded.items() if reason != "user_excluded"}
    if options.paths is not None and not set(options.paths) <= known:
        raise ValueError("selected_files_not_eligible")
    capacity = unit_capacity(options.limits)
    member_map = {key: member for key, member in members(changed_files).items()
                  if member["path"] not in excluded}
    member_cost = {key: tokens(member) for key, member in member_map.items()}
    path_members = {}
    for key, member in member_map.items():
        path_members.setdefault(member["path"], []).append((key, member))
    path_cost = {path: sum(member_cost[key] for key, _ in entries)
                 for path, entries in path_members.items()}
    if file_groups is None:
        if len(path_members) <= 3:
            volume = sum(change.additions + change.deletions for change in changed_files
                         if change.file_path in path_members)
            file_groups = ([list(path_members)] if volume < 200 else
                           [[path] for path in path_members])
        else:
            file_groups = [list(paths) for paths, _, _ in
                           _bundles(path_members, path_cost, relations, capacity)]
    groups, assigned = [], set()
    for proposed in file_groups:
        current, size = [], 0
        for path in proposed:
            if path not in path_members or path in assigned:
                continue
            cost = path_cost[path]
            if current and (len(current) >= MAX_UNIT_PATHS or size + cost > capacity):
                groups.append(current)
                current, size = [], 0
            current.append(path)
            size += cost
            assigned.add(path)
        if current:
            groups.append(current)
    groups.extend([path] for path in sorted(set(path_members) - assigned))
    units = []
    for group in groups:
        paths = sorted(group)
        risk, _ = _risk(paths, by_path, {})
        keys = [key for path in paths for key, _ in path_members[path]]
        unit_id = identity([snapshot.model_dump(), keys, options.focus, PLAN_VERSION])[:24]
        units.append(Unit(id=unit_id, paths=paths, required_hunk_ids=keys,
            risk=risk, focus=options.focus,
            estimated_input_tokens=sum(member_cost[key] for key in keys)))
    units.sort(key=lambda unit: ({"high": 0, "medium": 1, "low": 2}[unit.risk], unit.paths, unit.id))
    return Plan(version=PLAN_VERSION, units=units, excluded=excluded,
                relations=sorted(relations or []))
