"""Deterministic hunk packing without parser or model startup dependencies."""

import hashlib
import json
from functools import lru_cache

import tiktoken

from app.review.domain import Plan, Unit
from app.review.risk import _risk
from app.review.tool_scope import is_sensitive_repository_change


@lru_cache(maxsize=1)
def _encoding():
    return tiktoken.get_encoding("cl100k_base")


def tokens(value) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return len(_encoding().encode(text, disallowed_special=())) + 128


def identity(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def members(changed_files):
    """Split large hunks at source-line boundaries; never omit a changed member."""
    result = {}
    for change in changed_files:
        path = change.file_path
        for hunk in change.hunks:
            for start in range(0, len(hunk.lines), 120):
                lines = hunk.lines[start:start + 120]
                key = f"{hunk.hunk_id}:{start}"
                result[key] = {"id": key, "path": path, "old_path": change.old_file_path or path,
                    "lines": [line.model_dump() for line in lines]}
        if not change.hunks:
            key = f"structural-{identity(change.model_dump())[:20]}"
            result[key] = {"id": key, "path": path, "old_path": change.old_file_path or path,
                "change_type": change.change_type, "old_mode": change.old_mode,
                "new_mode": change.new_mode, "lines": []}
    return result


def plan_review(changed_files, snapshot, options) -> Plan:
    excluded, groups = {}, []
    by_path = {change.file_path: change.model_dump() for change in changed_files}
    for change in changed_files:
        path = change.file_path
        if change.is_binary:
            excluded[path] = "binary"
        elif is_sensitive_repository_change(path, change.old_file_path):
            excluded[path] = "sensitive_path"
        elif options.paths is not None and path not in options.paths:
            excluded[path] = "user_excluded"
    known = set(by_path) - {path for path, reason in excluded.items() if reason != "user_excluded"}
    if options.paths is not None and not set(options.paths) <= known:
        raise ValueError("selected_files_not_eligible")
    capacity = min(12000, max(1000, options.limits.total_input_tokens // 4))
    current, size = [], 0
    for key, member in members(changed_files).items():
        if member["path"] in excluded:
            continue
        cost = tokens(member)
        paths = {item[1]["path"] for item in current}
        if current and (size + cost > capacity or len(paths | {member["path"]}) > 4):
            groups.append(current)
            current, size = [], 0
        current.append((key, member))
        size += cost
    if current:
        groups.append(current)
    units = []
    for group in groups:
        paths = sorted({item["path"] for _, item in group})
        risk, _ = _risk(paths, by_path, {})
        keys = [key for key, _ in group]
        unit_id = identity([snapshot.model_dump(), keys, options.focus, "hunk-plan-v1"])[:24]
        units.append(Unit(id=unit_id, paths=paths, required_hunk_ids=keys,
            risk=risk, focus=options.focus, estimated_input_tokens=sum(tokens(item) for _, item in group)))
    units.sort(key=lambda unit: ({"high": 0, "medium": 1, "low": 2}[unit.risk], unit.paths, unit.id))
    return Plan(units=units, excluded=excluded)
