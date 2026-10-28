"""Deterministic review units, assignments and coverage goals."""

import json
import re
from dataclasses import dataclass

from app.review.configuration import DEPTHS, discovery_limits
from app.review.evidence import digest
from app.review.models import ReviewAssignment, ReviewGoal, ReviewUnit
from app.review.tool_scope import is_sensitive_repository_change

MAX_UNIT_PATHS = 4
MAX_UNIT_LINES = 800
MAX_UNIT_CHARS = 48_000
MAX_IMPACTED_PATHS = 20
RISK_PATTERN = re.compile(
    r"auth|permission|security|migrat|transact|lock|thread|async|pool|session", re.I)
GENERIC_TAGS = frozenset({
    "app", "backend", "frontend", "src", "lib", "core", "utils", "util", "common", "shared",
    "api", "routes", "routers", "controllers", "handlers", "services", "service", "models",
    "model", "schema", "schemas", "types", "config", "configs", "tests", "test", "spec",
    "specs", "index", "main", "init", "py", "ts", "js", "tsx", "jsx", "vue", "go", "java",
})
STRATEGY_LABELS = {
    "fast_pass": "快速单次 diff 审查，仅按需读取必要上下文",
    "contextual_review": "主审结合定向上下文调查，可疑问题进入独立核验",
    "deep_investigation": "跨模块影响分析，高风险与复杂变更增加独立审查，更严格反证",
}
ROLE_OBJECTIVES = {
    "reviewer": "综合检查变更前后的行为差异、异常路径与边界回归，以及调用者、消费者、数据结构与跨模块契约",
    "specialist": "检查本次变更中的权限、并发、事务和资源生命周期风险",
    "contract": "独立核对跨模块调用者、消费者与数据契约，挑战主审的接口假设",
}


@dataclass(frozen=True)
class ReviewPlan:
    goals: list[ReviewGoal]
    units: list[ReviewUnit]
    assignments: list[ReviewAssignment]
    excluded: list[str]
    limitations: list[str]
    uncovered: dict[str, str]


@dataclass
class _Unit:
    paths: list[str]
    hunk_ids: list[str]
    symbols: list[str]
    impacted: list[str]
    goals: list[str]
    risk: str
    reasons: list[str]
    strategy: str
    estimated: int
    roles: list[str]
    min_calls: int
    max_calls: int

    @property
    def id(self):
        return digest(json.dumps([self.paths, self.hunk_ids], ensure_ascii=False))


def _edge_files(edge):
    left = edge.get("source_file") or (edge.get("source") if edge.get("source_kind") == "file" else None)
    right = edge.get("target_file") or (edge.get("target") if edge.get("target_kind") == "file" else None)
    return left, right


def _changed_symbols(changes, symbols):
    result = {}
    for item in symbols:
        path = item.get("file")
        if path not in changes:
            continue
        if any(item.get("start_line", 0) <= hunk["new_start"] + hunk["new_length"] - 1
               and item.get("end_line", 0) >= hunk["new_start"]
               for hunk in changes[path].get("hunks", []) if hunk["new_length"]):
            identity = item.get("symbol_id") or f"{path}::{item.get('symbol')}"
            result.setdefault(path, set()).add(identity)
    return result


def _tags(path):
    return {tag for tag in re.split(r"[/_.-]", path.casefold())
            if len(tag) >= 3 and tag not in GENERIC_TAGS}


def _module(path):
    parts = path.split("/")[:-1]
    index = 0
    while index < len(parts) - 1 and parts[index].casefold() in {"app", "backend", "frontend", "src", "lib"}:
        index += 1
    return "/".join(parts[:index + 1])


def _links(changes, repository_graph, changed_symbols):
    links = []
    changed = set().union(*changed_symbols.values())
    for edge in repository_graph.get("edges", []):
        left, right = _edge_files(edge)
        if not left or not right or left not in changes or right not in changes or left == right:
            continue
        kind = edge.get("type")
        if (kind == "calls" and edge.get("resolution_status") == "resolved"
                and edge.get("relationship_level") != "search_hint"
                and {edge.get("source"), edge.get("target")} & changed):
            priority = 0
        elif kind == "imports" and edge.get("confidence", 0) >= 0.9:
            priority = 1
        elif kind == "test_of" and edge.get("confidence", 0) >= 0.9:
            priority = 2
        else:
            continue
        links.append((priority, -edge.get("confidence", 0), *sorted((left, right))))
    # Directory and shared feature tokens are weak hints, ranked below code edges.
    for paths in _index(changes, _tags).values():
        for left, right in zip(paths, paths[1:]):
            links.append((4, 0, *sorted((left, right))))
    for paths in _index(changes, lambda path: (
            {path.rsplit("/", 1)[0]} if "/" in path else set())).values():
        for left, right in zip(paths, paths[1:]):
            links.append((3, 0, *sorted((left, right))))
    return sorted(links)


def _index(changes, tags):
    index = {}
    for path in sorted(changes):
        for tag in tags(path):
            index.setdefault(tag, []).append(path)
    return index


def _groups(changes, repository_graph, changed_symbols):
    groups = {path: {path} for path in changes}
    owners = {path: path for path in changes}
    for _, _, left, right in _links(changes, repository_graph, changed_symbols):
        a, b = owners[left], owners[right]
        if a == b:
            continue
        combined = groups[a] | groups[b]
        volume = sum(hunk["old_length"] + hunk["new_length"]
                     for path in combined for hunk in changes[path].get("hunks", []))
        characters = sum(len(line.get("content", "")) for path in combined
                         for hunk in changes[path].get("hunks", [])
                         for line in hunk.get("lines", []))
        if (len(combined) > MAX_UNIT_PATHS or volume > MAX_UNIT_LINES
                or characters > MAX_UNIT_CHARS):
            continue
        keep, remove = sorted((a, b))
        groups[keep] = combined
        del groups[remove]
        for path in combined:
            owners[path] = keep
    return sorted(sorted(group) for group in groups.values())


def _impacted_paths(changes, repository_graph):
    impacted = {}
    for edge in repository_graph.get("edges", []):
        left, right = _edge_files(edge)
        if left in changes and right and right not in changes:
            impacted.setdefault(left, set()).add(right)
        if right in changes and left and left not in changes:
            impacted.setdefault(right, set()).add(left)
    return {path: sorted(paths)[:MAX_IMPACTED_PATHS] for path, paths in impacted.items()}


def _risk(paths, changes):
    text = " ".join(paths) + " " + " ".join(
        line.get("content", "") for path in paths for hunk in changes[path].get("hunks", [])
        for line in hunk.get("lines", []))
    matches = sorted({match.group(0).casefold() for match in RISK_PATTERN.finditer(text)})
    reasons = [f"risk_keyword:{match}" for match in matches]
    cross_module = len({_module(path) for path in paths}) > 1
    volume = sum(hunk["old_length"] + hunk["new_length"]
                 for path in paths for hunk in changes[path].get("hunks", []))
    if cross_module:
        reasons.append("cross_module")
    if volume >= 200:
        reasons.append("large_change")
    if any(changes[path].get("change_type") in {"added", "deleted"} for path in paths):
        reasons.append("file_added_or_deleted")
    if matches:
        return "high", reasons
    if cross_module or volume >= 200 or "file_added_or_deleted" in reasons:
        return "medium", reasons
    return "low", reasons


def _roles(paths, risk, depth):
    roles = ["reviewer"]
    if depth in {"standard", "deep"} and risk == "high":
        roles.append("specialist")
    if (depth == "deep" and risk != "low" and len(paths) >= 3
            and len({_module(path) for path in paths}) > 1):
        roles.append("contract")
    return roles


def _estimate(paths, changes):
    encoded = sum(len(line.get("content", "").encode("utf-8"))
                  for path in paths for hunk in changes[path].get("hunks", [])
                  for line in hunk.get("lines", []))
    return max(1, encoded + 2000 * len(paths))


def _unit(paths, changes, by_path, symbols, impacted, depth, limits):
    impacted_paths = sorted({path for source in paths for path in impacted.get(source, [])})
    impacted_paths = impacted_paths[:MAX_IMPACTED_PATHS]
    hunk_ids = []
    for path in paths:
        for hunk in changes[path].get("hunks", []):
            if hunk.get("old_length") or hunk.get("new_length"):
                hunk_ids.append(hunk.get("hunk_id") or digest(
                    f"{path}:{hunk['old_start']}:{hunk['old_length']}:"
                    f"{hunk['new_start']}:{hunk['new_length']}"))
    risk, reasons = _risk(paths, changes)
    roles = _roles(paths, risk, depth)
    min_each = 1 if depth == "quick" else 2
    max_each = limits.model_calls if limits is not None else DEPTHS[depth]["model_calls"]
    return _Unit(paths=paths, hunk_ids=sorted(hunk_ids),
        symbols=sorted({identity for path in paths for identity in symbols.get(path, set())}),
        impacted=impacted_paths, goals=sorted({goal.id for path in paths for goal in by_path[path]
                                         if goal.status != "blocked"}),
        risk=risk, reasons=reasons, strategy=DEPTHS[depth]["strategy"],
        estimated=_estimate(paths, changes), roles=roles,
        min_calls=min_each * len(roles), max_calls=max_each * len(roles))


def _paths_text(paths):
    shown = "、".join(paths[:4])
    return shown + (f" 等 {len(paths)} 个文件" if len(paths) > 4 else "")


def _priority(unit):
    return ({"high": 0, "medium": 1, "low": 2}[unit.risk], -unit.estimated, unit.paths)


def _merge(left, right, changes, depth):
    paths = sorted(set(left.paths) | set(right.paths))
    if len(paths) > MAX_UNIT_PATHS:
        return None
    volume = sum(hunk["old_length"] + hunk["new_length"]
                 for path in paths for hunk in changes[path].get("hunks", []))
    characters = sum(len(line.get("content", "")) for path in paths
                     for hunk in changes[path].get("hunks", []) for line in hunk.get("lines", []))
    if volume > MAX_UNIT_LINES or characters > MAX_UNIT_CHARS:
        return None
    min_each = 1 if depth == "quick" else 2
    max_each = left.max_calls // max(1, len(left.roles))
    return _Unit(paths=paths, hunk_ids=sorted(set(left.hunk_ids) | set(right.hunk_ids)),
        symbols=sorted(set(left.symbols) | set(right.symbols)),
        impacted=sorted(set(left.impacted) | set(right.impacted))[:MAX_IMPACTED_PATHS],
        goals=sorted(set(left.goals) | set(right.goals)),
        risk=max((left.risk, right.risk), key=lambda tier: {"low": 0, "medium": 1, "high": 2}[tier]),
        reasons=sorted(set(left.reasons) | set(right.reasons)), strategy=left.strategy,
        estimated=left.estimated + right.estimated, roles=["reviewer"],
        min_calls=min_each, max_calls=max_each)


def _pack(units, changes, depth):
    units = sorted(units, key=_priority)
    changed = True
    while changed:
        changed = False
        for index, left in enumerate(units):
            if left.risk == "high":
                continue
            for offset in range(index + 1, len(units)):
                right = units[offset]
                if right.risk == "high":
                    continue
                merged = _merge(left, right, changes, depth)
                if merged is None:
                    continue
                units = [unit for position, unit in enumerate(units)
                         if position not in {index, offset}] + [merged]
                units.sort(key=_priority)
                changed = True
                break
            if changed:
                break
    return units


def _fit(units, changes, depth, limits):
    if limits is None or not units:
        return units, [], {}
    limitations, uncovered = [], {}
    discovery = discovery_limits(limits, depth)
    capacity = discovery.total_model_calls
    cost = sum(unit.min_calls for unit in units)
    if cost > capacity:
        dropped = False
        for role in ("contract", "specialist"):
            if cost <= capacity:
                break
            for unit in units:
                if role not in unit.roles or len(unit.roles) == 1:
                    continue
                unit.roles.remove(role)
                unit.min_calls -= 1 if depth == "quick" else 2
                unit.max_calls -= limits.model_calls
                dropped = True
            cost = sum(unit.min_calls for unit in units)
        if dropped:
            limitations.append("预算不足：部分高风险或跨模块单元的专项/独立审查未启动。")
    if cost > capacity:
        units = _pack(units, changes, depth)
        cost = sum(unit.min_calls for unit in units)
    if cost > capacity:
        kept, remaining = [], capacity
        for unit in sorted(units, key=_priority):
            if unit.min_calls <= remaining:
                kept.append(unit)
                remaining -= unit.min_calls
            else:
                limitations.append("未覆盖（整轮模型调用预算不足）：" + _paths_text(unit.paths))
                uncovered.update((path, "model_call_budget_insufficient") for path in unit.paths)
        units = sorted(kept, key=lambda unit: unit.paths)
    token_capacity = discovery.total_input_tokens
    if sum(unit.estimated for unit in units) > token_capacity:
        kept, remaining = [], token_capacity
        for unit in sorted(units, key=_priority):
            if unit.estimated <= remaining:
                kept.append(unit)
                remaining -= unit.estimated
            else:
                limitations.append("未覆盖（累计输入预算不足）：" + _paths_text(unit.paths))
                uncovered.update((path, "input_budget_insufficient") for path in unit.paths)
        units = sorted(kept, key=lambda unit: unit.paths)
    return units, limitations, uncovered


def _assignments(unit, depth, focus):
    output = []
    for role in unit.roles:
        objective = ROLE_OBJECTIVES[role]
        if len(unit.paths) > 1:
            objective += "；联合核对相关变更，不把静态关联当作源码证据"
        objective += (f"；审查深度：{depth}（{STRATEGY_LABELS[unit.strategy]}）"
                      f"；重点：{', '.join(focus)}")
        output.append(ReviewAssignment(
            id=digest(json.dumps([unit.id, role, unit.goals, objective], ensure_ascii=False)),
            unit_id=unit.id, reviewer=role, goal_ids=unit.goals,
            focus_paths=unit.paths, objective=objective))
    return output


def plan_review(changed_files: list[dict], *, depth: str, selected_paths=None, focus=(),
                repository_graph=None, symbols=(), limits=None) -> ReviewPlan:
    goals, excluded, included, by_path = [], [], {}, {}
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
    graph = repository_graph or {}
    changed_symbols = _changed_symbols(included, symbols)
    impacted = _impacted_paths(included, graph)
    units, limitations, uncovered = [], [], {}
    for paths in _groups(included, graph, changed_symbols):
        unit = _unit(paths, included, by_path, changed_symbols, impacted, depth, limits)
        if not unit.goals and not unit.hunk_ids:
            limitations.append("无文本变更范围，未派发审查：" + "、".join(paths))
            uncovered.update((path, "no_text_source_range") for path in paths)
            continue
        units.append(unit)
    units, budget_limitations, budget_uncovered = _fit(units, included, depth, limits)
    assignments = [assignment for unit in units for assignment in _assignments(unit, depth, focus)]
    unique_goals = {goal.id: goal for goal in goals}
    return ReviewPlan(goals=list(unique_goals.values()),
        units=[ReviewUnit(id=unit.id, changed_paths=unit.paths, changed_hunk_ids=unit.hunk_ids,
            changed_symbols=unit.symbols, impacted_paths=unit.impacted, risk_tier=unit.risk,
            risk_reasons=unit.reasons, strategy=unit.strategy,
            estimated_input_tokens=unit.estimated, min_model_calls=unit.min_calls,
            max_model_calls=unit.max_calls, goal_ids=unit.goals) for unit in units],
        assignments=assignments, excluded=excluded,
        limitations=limitations + budget_limitations,
        uncovered={**uncovered, **budget_uncovered})
