"""Deterministic review units, assignments and coverage goals."""

import json
import re
from dataclasses import dataclass

from app.review.configuration import DEPTHS, discovery_limits
from app.review.evidence import digest
from app.review.models import ReviewAssignment, ReviewGoal, ReviewUnit
from app.review.tool_scope import is_sensitive_repository_change, is_sensitive_repository_path

MAX_UNIT_PATHS = 4
MAX_UNIT_LINES = 800
MAX_UNIT_CHARS = 48_000
MAX_IMPACTED_PATHS = 20
REFERENCEABLE_SUFFIXES = (
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".java", ".go", ".rs", ".vue", ".svelte",
)
CONFIG_NAMES = frozenset({
    "pyproject.toml", "setup.cfg", "setup.py", "tox.ini", "pytest.ini",
    "requirements.txt", "package.json", "tsconfig.json", "vite.config.ts",
    "vite.config.js", "go.mod", "cargo.toml", "pom.xml", "build.gradle",
    "build.gradle.kts", "makefile", "pipfile",
})
RISK_PATH_GROUPS = {
    "auth": frozenset({
        "auth", "authentication", "authorization", "permission", "permissions",
        "oauth", "oidc", "saml", "password", "passwords", "token", "tokens",
        "session", "sessions", "credential", "credentials", "security",
        "secret", "secrets",
    }),
    "crypto": frozenset({"crypto", "cryptography", "encryption", "signing"}),
    "money": frozenset({
        "payment", "payments", "billing", "invoice", "invoices", "money",
        "price", "pricing", "currency", "refund", "ledger",
    }),
    "data": frozenset({
        "migration", "migrations", "schema", "schemas", "transaction", "transactions",
    }),
    "concurrency": frozenset({
        "concurrency", "worker", "workers", "queue", "queues", "lock", "locks",
    }),
    "supply_chain": frozenset({
        "requirements", "dockerfile", "workflows", "dependencies", "dependency",
    }),
    "storage": frozenset({"storage", "upload", "uploads", "download", "downloads"}),
}
RISK_OPERATION_GROUPS = {
    "crypto": re.compile(
        r"\b(?:hmac|hashlib|bcrypt|scrypt|argon2|cryptography)\b"
        r"|\b(?:encrypt|decrypt|compare_digest)\s*\(", re.I),
    "deserialization": re.compile(
        r"\b(?:pickle|marshal)\b|\byaml\.(?:un)?safe_load\b", re.I),
    "command_execution": re.compile(
        r"\b(?:subprocess|os\.system|os\.popen)\b|\b(?:eval|exec)\s*\(|shell\s*=\s*True",
        re.I),
    "sql": re.compile(
        r"\b(?:execute|executemany)\s*\(|\b(?:BEGIN|COMMIT|ROLLBACK)\b", re.I),
    "path": re.compile(r"\bos\.path\b|\b(?:realpath|normpath)\b", re.I),
    "concurrency": re.compile(
        r"\b(?:threading|multiprocessing|RLock|Semaphore)\b|\b(?:Lock|gather|wait)\s*\(",
        re.I),
    "money": re.compile(r"\bDecimal\b", re.I),
}
PUBLIC_API_PARTS = frozenset({"api", "contracts", "openapi", "public", "sdk"})
PUBLIC_API_NAMES = frozenset({"__init__.py", "index.ts", "index.js", "index.tsx", "index.jsx"})
DOCUMENTATION_SUFFIXES = (".md", ".mdx", ".rst", ".adoc", ".txt")
QUOTED_TEXT = r"'(?:\\.|[^'\\\n])*'" r'|"(?:\\.|[^"\\\n])*"'
PYTHON_QUOTED_TEXT = (
    r"'''[\s\S]*?(?:'''|\Z)" + r'|"""[\s\S]*?(?:"""|\Z)|' + QUOTED_TEXT)
PYTHON_NON_CODE = re.compile(
    r"(?P<dynamic>\b(?i:f|fr|rf)(?:" + PYTHON_QUOTED_TEXT + r"))"
    r"|(?:\b(?i:r|u|b|br|rb))?(?:" + PYTHON_QUOTED_TEXT + r")|#[^\n]*")
C_STYLE_NON_CODE = re.compile(
    r"/\*[\s\S]*?(?:\*/|\Z)|//[^\n]*|<!--[\s\S]*?(?:-->|\Z)|"
    r'"""[\s\S]*?(?:"""|\Z)|(?P<dynamic>`[\s\S]*?(?:`|\Z))|' + QUOTED_TEXT)
SQL_NON_CODE = re.compile(r"--[^\n]*|" + C_STYLE_NON_CODE.pattern)
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
    structural: bool = False

    @property
    def id(self):
        return digest(json.dumps([self.paths, self.hunk_ids], ensure_ascii=False))


def _edge_files(edge):
    left = edge.get("source_file") or (edge.get("source") if edge.get("source_kind") == "file" else None)
    right = edge.get("target_file") or (edge.get("target") if edge.get("target_kind") == "file" else None)
    return left, right


def _structural_goal(change):
    """Structural checks have no source range; callers must be read as evidence."""
    path, kind, old = change["file_path"], change.get("change_type"), change.get("old_file_path")
    if kind == "renamed" and old:
        return ReviewGoal(id=digest(f"structural:rename:{old}:{path}"), kind="structural",
            path=path, side="head",
            description=f"检查是否仍有导入、模块引用或配置引用指向旧路径 {old}；读取实际引用者源码确认")
    if kind == "deleted" and (path.casefold().endswith(REFERENCEABLE_SUFFIXES)
                              or path.rsplit("/", 1)[-1].casefold() in CONFIG_NAMES):
        return ReviewGoal(id=digest(f"structural:delete:{path}"), kind="structural",
            path=path, side="base",
            description=f"检查是否仍有导入、模块引用或配置引用指向已删除的 {path}；读取实际引用者源码确认")
    return None


def _code_lines(path, lines):
    path = path.casefold()
    if path.endswith(DOCUMENTATION_SUFFIXES):
        return [""] * len(lines)
    if path.endswith(".sql"):
        pattern = SQL_NON_CODE
    elif path.endswith((".py", ".pyi", ".sh", ".bash")) or path.rsplit("/", 1)[-1] == "dockerfile":
        pattern = PYTHON_NON_CODE
    else:
        pattern = C_STYLE_NON_CODE
    text = "\n".join(line.get("content", "") for line in lines)
    # Interpolated strings may execute code; keep them as conservative lexical hints.
    return pattern.sub(lambda match: match.group() if match.group("dynamic") else
                       re.sub(r"[^\n]", " ", match.group()), text).split("\n")


def _changed_code(change):
    for hunk in change.get("hunks", []):
        lines = hunk.get("lines", [])
        head = iter(_code_lines(change["file_path"],
                               [line for line in lines if line.get("kind") != "deleted"]))
        base = iter(_code_lines(change["file_path"],
                               [line for line in lines if line.get("kind") != "added"]))
        head_line = hunk["new_start"] + (1 if not hunk["new_length"] else 0)
        for line in lines:
            kind = line.get("kind")
            old_code = next(base) if kind != "added" else ""
            new_code = next(head) if kind != "deleted" else ""
            if kind in {"added", "deleted"}:
                code = new_code if kind == "added" else old_code
                if code.strip():
                    yield kind, head_line, code
            if kind != "deleted":
                head_line += 1


def _changed_symbols(changes, symbols):
    result, changed_lines = {}, {}
    for item in symbols:
        path = item.get("file")
        if path not in changes:
            continue
        if path not in changed_lines:
            changed_lines[path] = {(kind, number) for kind, number, _ in _changed_code(changes[path])}
        # Deleted lines are anchored before the next HEAD line, not inside a symbol starting there.
        if any(item.get("start_line", 0) <= number <= item.get("end_line", 0)
               and (kind == "added" or item.get("start_line", 0) < number)
               for kind, number in changed_lines[path]):
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
            if not is_sensitive_repository_path(right):
                impacted.setdefault(left, set()).add(right)
        if right in changes and left and left not in changes:
            if not is_sensitive_repository_path(left):
                impacted.setdefault(right, set()).add(left)
    return {path: sorted(paths)[:MAX_IMPACTED_PATHS] for path, paths in impacted.items()}


def _source_or_config(path):
    return path.casefold().endswith(REFERENCEABLE_SUFFIXES) or (
        path.rsplit("/", 1)[-1].casefold() in CONFIG_NAMES)


def _public_entry(path):
    parts = path.casefold().split("/")
    return parts[-1] in PUBLIC_API_NAMES or any(
        part in PUBLIC_API_PARTS for part in parts[:-1])


def _risk(paths, changes, changed_symbols):
    segments = {segment for path in paths
                if any(hunk["old_length"] or hunk["new_length"]
                       for hunk in changes[path].get("hunks", []))
                for segment in re.split(r"[/_.-]", path.casefold()) if segment}
    volume = sum(hunk["old_length"] + hunk["new_length"]
                 for path in paths for hunk in changes[path].get("hunks", []))
    # A pure rename carries no text change; structural references cover it.
    path_hits = sorted(group for group, words in RISK_PATH_GROUPS.items()
                       if segments & words)
    added = "\n".join(code for path in paths
                      for kind, _, code in _changed_code(changes[path]) if kind == "added")
    operation_hits = sorted(group for group, pattern in RISK_OPERATION_GROUPS.items()
                            if pattern.search(added))
    public_api = any(_public_entry(path) and changed_symbols.get(path) for path in paths)
    cross_module = len({_module(path) for path in paths}) > 1
    file_event = any(changes[path].get("change_type") in {"added", "deleted"}
                     and _source_or_config(path) for path in paths)
    structural = any(changes[path].get("change_type") == "renamed" for path in paths)
    reasons = [f"sensitive_path:{group}" for group in path_hits]
    reasons += [f"sensitive_operation:{group}" for group in operation_hits]
    if public_api:
        reasons.append("public_api_change")
    if cross_module:
        reasons.append("cross_module")
    if volume >= 200:
        reasons.append("large_change")
    if file_event:
        reasons.append("file_added_or_deleted")
    if structural:
        reasons.append("structural_change")
    score = (2 if path_hits else 0) + (2 if operation_hits else 0) + (2 if public_api else 0)
    score += (1 if cross_module else 0) + (1 if file_event else 0) + (1 if structural else 0)
    if volume >= 200:
        score += 2 if volume >= 800 else 1
    if score >= 2 and (path_hits or operation_hits or public_api):
        return "high", reasons
    return ("medium" if score >= 1 else "low"), reasons


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
    risk, reasons = _risk(paths, changes, symbols)
    roles = _roles(paths, risk, depth)
    min_each = 1 if depth == "quick" else 2
    max_each = limits.model_calls if limits is not None else DEPTHS[depth]["model_calls"]
    structural = any(goal.kind == "structural" for path in paths for goal in by_path[path])
    return _Unit(paths=paths, hunk_ids=sorted(hunk_ids),
        symbols=sorted({identity for path in paths for identity in symbols.get(path, set())}),
        impacted=impacted_paths, goals=sorted({goal.id for path in paths for goal in by_path[path]
                                         if goal.status != "blocked"}),
        risk=risk, reasons=reasons, strategy=DEPTHS[depth]["strategy"],
        estimated=_estimate(paths, changes), roles=roles,
        min_calls=min_each * len(roles), max_calls=max_each * len(roles), structural=structural)


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
        min_calls=min_each, max_calls=max_each,
        structural=left.structural or right.structural)


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
        if unit.symbols:
            objective += "；变更符号：" + ", ".join(unit.symbols[:8])
        if unit.impacted:
            objective += ("；静态关联影响路径（需读取源码核验）："
                          + "、".join(unit.impacted[:8]))
        if unit.structural:
            objective += ("；结构性变更：核对旧路径的导入与模块引用，引用实际调用者源码，"
                          "不得伪造行级证据")
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
        structural = _structural_goal(change)
        if structural:
            file_goals.append(structural)
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
