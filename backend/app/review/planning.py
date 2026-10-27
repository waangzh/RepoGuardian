"""Deterministic assignments and coverage goals, independent of model protocols."""

import json
import re

from app.review.evidence import digest
from app.review.models import ReviewAssignment, ReviewGoal
from app.review.tool_scope import is_sensitive_repository_change


def _test_path(path):
    return bool(re.search(r"(^|/)(tests?|__tests__|specs?)/|(^|/)test_|[._](test|spec)[._]", path))


def _api_model_pair(left, right):
    def tags(path):
        return set(re.split(r"[/_.-]", path.casefold()))
    api = {"api", "routes", "routers", "controllers", "handlers"}
    model = {"model", "models", "schema", "schemas", "dto", "types"}
    return bool((tags(left) & api and tags(right) & model)
                or (tags(right) & api and tags(left) & model))


def _groups(changes, repository_graph, symbols):
    groups = {path: {path} for path in changes}
    owners = {path: path for path in changes}
    changed_symbols = {item.get("symbol_id") or f"{item['file']}::{item.get('symbol')}"
        for item in symbols if item.get("file") in changes
        and any(item.get("start_line", 0) <= hunk["new_start"] + hunk["new_length"] - 1
                and item.get("end_line", 0) >= hunk["new_start"]
                for hunk in changes[item["file"]].get("hunks", []) if hunk["new_length"])}
    links = []
    for edge in repository_graph.get("edges", []):
        left = edge.get("source_file") or (edge.get("source") if edge.get("source_kind") == "file" else None)
        right = edge.get("target_file") or (edge.get("target") if edge.get("target_kind") == "file" else None)
        if left not in changes or right not in changes or left == right:
            continue
        kind = edge.get("type")
        if (kind == "calls" and edge.get("resolution_status") == "resolved"
                and edge.get("relationship_level") != "search_hint"
                and {edge.get("source"), edge.get("target")} & changed_symbols):
            priority = 0
        elif (kind == "imports" and edge.get("confidence", 0) >= 0.95 and (
                (_test_path(left) and not _test_path(right))
                or (_test_path(right) and not _test_path(left))
                or _api_model_pair(left, right))):
            priority = 1
        elif kind == "test_of" and edge.get("confidence", 0) >= 0.9:
            priority = 2
        else:
            continue
        links.append((priority, -edge.get("confidence", 0), *sorted((left, right))))
    for _, _, left, right in sorted(links):
        a, b = owners[left], owners[right]
        if a == b:
            continue
        combined = groups[a] | groups[b]
        volume = sum(hunk["old_length"] + hunk["new_length"]
            for path in combined for hunk in changes[path].get("hunks", []))
        characters = sum(len(line.get("content", "")) for path in combined
            for hunk in changes[path].get("hunks", []) for line in hunk.get("lines", []))
        if len(combined) > 4 or volume > 800 or characters > 48_000:
            continue
        keep, remove = sorted((a, b))
        groups[keep] = combined
        del groups[remove]
        for path in combined:
            owners[path] = keep
    return sorted(sorted(group) for group in groups.values())


def plan_review(changed_files: list[dict], *, depth: str, selected_paths=None, focus=(),
                repository_graph=None, symbols=()):
    goals, assignments, excluded = [], [], []
    included, by_path = {}, {}
    for change in sorted(changed_files, key=lambda item: item["file_path"]):
        path = change["file_path"]
        if (is_sensitive_repository_change(path, change.get("old_file_path"))
                or change.get("is_binary") or (selected_paths is not None and path not in selected_paths)):
            excluded.append(path)
            continue
        file_goals = []
        for hunk in change.get("hunks", []):
            for side in ("head", "base"):
                prefix = "new" if side == "head" else "old"
                start, length = hunk[f"{prefix}_start"], hunk[f"{prefix}_length"]
                source_path = (change.get("old_file_path") or path) if side == "base" else path
                for offset in range(0, length, 200):
                    first, last = start + offset, start + min(length, offset + 200) - 1
                    identity = digest(f"{source_path}:{side}:{first}:{last}")
                    file_goals.append(ReviewGoal(id=identity, path=source_path, side=side,
                                                 start_line=first, end_line=last))
        if not file_goals:
            # Metadata/binary changes cannot acquire fictional line evidence.
            file_goals.append(ReviewGoal(id=digest(path + ":metadata"), path=path, side="head",
                start_line=1, end_line=1, status="blocked", reason="no_text_source_range"))
        goals.extend(file_goals)
        included[path], by_path[path] = change, file_goals
    for paths in _groups(included, repository_graph or {}, symbols):
        file_goals = [goal for path in paths for goal in by_path[path]]
        roles = ["behavior", "contract"]
        risk = re.search(r"auth|permission|security|migrat|transact|lock|thread|async|pool|session",
                         " ".join(paths) + " " + " ".join(line.get("content", "")
                         for path in paths for hunk in included[path].get("hunks", [])
                         for line in hunk.get("lines", [])), re.I)
        if risk:
            roles.append("specialist")
        for role in roles:
            goal_ids = sorted({goal.id for goal in file_goals if goal.status != "blocked"})
            objective = {
                "behavior": "检查变更前后的行为差异、异常路径与边界回归",
                "contract": "检查调用者、消费者、数据结构与跨模块契约",
                "specialist": "检查本次变更中的权限、并发、事务和资源生命周期风险",
            }[role] + ("；联合核对相关变更，不把静态关联当作源码证据" if len(paths) > 1 else "")
            objective += f"；审查深度：{depth}；重点：{', '.join(focus)}"
            assignments.append(ReviewAssignment(
                id=digest(json.dumps([paths, role, goal_ids, objective], ensure_ascii=False)),
                reviewer=role, goal_ids=goal_ids, focus_paths=paths, objective=objective,
            ))
    unique_goals = {goal.id: goal for goal in goals}
    return list(unique_goals.values()), assignments, excluded
