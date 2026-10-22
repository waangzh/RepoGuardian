"""静态符号上下文预取。

在模型进入工具循环之前，把当前目标变更行里引用的仓库内定义
（文件、行号、签名、docstring）作为检索提示注入请求。提示只来自
已构建的符号索引，不读取新文件，不进入证据目录，也不能被直接引用。
"""

from __future__ import annotations

import re

from app.review.tool_scope import is_sensitive_repository_path

MAX_DEFINITIONS = 8
MAX_SIGNATURE_CHARS = 240
MAX_DOCSTRING_CHARS = 320
MAX_TOTAL_CHARS = 4_000
MAX_SCANNED_LINES = 400

_CALL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{1,80})\s*\(")
_TYPE = re.compile(r"\b([A-Z][A-Za-z0-9_]{1,80})\b")
_KEYWORDS = frozenset({
    "if", "for", "while", "switch", "catch", "return", "func", "fn", "function",
    "def", "class", "with", "except", "assert", "lambda", "raise", "yield", "await",
    "async", "match", "case", "elif", "else", "try", "finally", "import", "from",
    "not", "and", "or", "in", "is", "del", "pass", "break", "continue", "global",
    "nonlocal", "new", "delete", "typeof", "instanceof", "void", "this", "super",
    "self", "cls", "print", "len", "str", "int", "float", "bool", "list", "dict",
    "set", "tuple", "range", "enumerate", "zip", "isinstance", "getattr", "setattr",
    "Exception", "ValueError", "TypeError", "KeyError", "RuntimeError",
})
_RELATION_RANK = {"resolved_call": 0, "same_file": 1, "imported": 2, "unique_name": 3}


def _changed_lines(goals, active_evidence_ids, catalog) -> list[str]:
    """只扫描当前目标的变更行；分页时仍使用目标自身的规范 diff。"""
    goal_ids = {goal.goal_id for goal in goals}
    goal_files = {goal.target_ref for goal in goals}
    changed: list[str] = []
    for identity in sorted(catalog):
        change = (catalog.get(identity) or {}).get("canonical_change")
        if not isinstance(change, dict) or change.get("goal_id") not in goal_ids:
            continue
        for line in change.get("lines") or []:
            if (isinstance(line, dict) and line.get("kind") in {"added", "deleted"}
                    and isinstance(line.get("content"), str)):
                changed.append(line["content"])
    if changed:
        return changed[:MAX_SCANNED_LINES]
    fallback: list[str] = []
    for identity in sorted(active_evidence_ids):
        value = catalog.get(identity) or {}
        if value.get("file_path") in goal_files and isinstance(value.get("content"), str):
            fallback.extend(value["content"].splitlines())
    return fallback[:MAX_SCANNED_LINES]


def _referenced_names(lines: list[str]) -> list[str]:
    names: dict[str, None] = {}
    for line in lines:
        for pattern in (_CALL, _TYPE):
            for name in pattern.findall(line):
                if len(name) < 2 or name.casefold() in _KEYWORDS:
                    continue
                names.setdefault(name, None)
    return sorted(names)


def _symbol_id(symbol: dict) -> str:
    return str(symbol.get("symbol_id") or f"{symbol.get('file')}::{symbol.get('symbol')}")


def _valid_range(symbol: dict) -> bool:
    start, end = symbol.get("start_line"), symbol.get("end_line")
    return type(start) is int and type(end) is int and 1 <= start <= end <= 1_000_000


def _eligible(symbol: dict, readable_files: set[str]) -> bool:
    path = symbol.get("file")
    return (isinstance(path, str) and path in readable_files
            and not is_sensitive_repository_path(path)
            and isinstance(symbol.get("symbol"), str) and _valid_range(symbol))


def _overlaps_goal(symbol: dict, goal_files: set[str], goals) -> bool:
    if symbol["file"] not in goal_files:
        return False
    return any(symbol["file"] == goal.target_ref
               and symbol["start_line"] <= goal.end_line
               and symbol["end_line"] >= goal.start_line for goal in goals)


def _covered_by_active(symbol: dict, active_ranges: dict[str, list[tuple[int, int]]]) -> bool:
    return any(start <= symbol["start_line"] and symbol["end_line"] <= end
               for start, end in active_ranges.get(symbol["file"], ()))


def _resolved_targets(goals, repository_graph: dict) -> tuple[set[str], set[str]]:
    """返回已解析调用目标符号 ID 与目标文件的导入文件集合。"""
    goal_files = {goal.target_ref for goal in goals}
    targets: set[str] = set()
    imported: set[str] = set()
    for edge in (repository_graph or {}).get("edges", []):
        source_file = edge.get("source_file") or str(edge.get("source") or "").split("::", 1)[0]
        if source_file not in goal_files:
            continue
        if edge.get("type") == "imports":
            target_file = edge.get("target_file") or str(edge.get("target") or "").split("::", 1)[0]
            if target_file:
                imported.add(str(target_file))
        elif (edge.get("type") == "calls" and edge.get("resolution_status") == "resolved"
              and edge.get("relationship_level") != "search_hint"):
            targets.add(str(edge.get("target") or ""))
    return targets, imported


def _entry(symbol: dict, relation: str) -> dict:
    signature = " ".join(str(symbol.get("signature") or "").split())[:MAX_SIGNATURE_CHARS]
    docstring = " ".join(str(symbol.get("docstring") or "").split())[:MAX_DOCSTRING_CHARS]
    return {
        "file_path": symbol["file"],
        "symbol": symbol["symbol"],
        "kind": str(symbol.get("type") or "unknown"),
        "start_line": symbol["start_line"],
        "end_line": symbol["end_line"],
        "signature": signature,
        "docstring": docstring or None,
        "relation": relation,
        "source": "static_symbol_index",
    }


def build_definition_hints(*, goals, active_evidence_ids, catalog, symbol_index,
                           repository_graph, readable_files) -> list[dict]:
    """按确定性排序返回最多 MAX_DEFINITIONS 条定义提示；歧义定义一律跳过。"""
    if not goals or not symbol_index or not readable_files:
        return []
    names = _referenced_names(_changed_lines(goals, active_evidence_ids, catalog))
    if not names:
        return []
    readable = set(readable_files)
    goal_files = {goal.target_ref for goal in goals}
    wanted = set(names)
    # 单遍扫描：先按名称筛掉绝大多数条目，再对少量候选做范围与敏感路径校验。
    by_name: dict[str, list[dict]] = {}
    for symbol in symbol_index:
        full = symbol.get("symbol")
        if not isinstance(full, str):
            continue
        simple = full.rsplit(".", 1)[-1]
        if simple not in wanted and full not in wanted:
            continue
        if not _eligible(symbol, readable):
            continue
        if simple in wanted:
            by_name.setdefault(simple, []).append(symbol)
        if full in wanted and full != simple:
            by_name.setdefault(full, []).append(symbol)
    if not by_name:
        return []
    active_ranges: dict[str, list[tuple[int, int]]] = {}
    for identity in active_evidence_ids:
        value = catalog.get(identity) or {}
        if (isinstance(value.get("file_path"), str) and isinstance(value.get("start_line"), int)
                and isinstance(value.get("end_line"), int)):
            active_ranges.setdefault(value["file_path"], []).append(
                (value["start_line"], value["end_line"]))
    resolved, imported = _resolved_targets(goals, repository_graph)

    def unique(candidates: list[dict]) -> list[dict]:
        seen, output = set(), []
        for symbol in candidates:
            key = (symbol["file"], symbol["symbol"], symbol["start_line"])
            if key not in seen:
                seen.add(key)
                output.append(symbol)
        return output

    def candidate(symbol: dict, relation: str) -> dict:
        return _entry(symbol, relation)

    selected: list[dict] = []
    for name in names:
        candidates = [symbol for symbol in by_name.get(name, [])
                      if not _overlaps_goal(symbol, goal_files, goals)
                      and not _covered_by_active(symbol, active_ranges)]
        if not candidates:
            continue
        resolved_candidates = unique([symbol for symbol in candidates
                                      if _symbol_id(symbol) in resolved])
        if len(resolved_candidates) == 1:
            selected.append(candidate(resolved_candidates[0], "resolved_call"))
            continue
        same_file = unique([symbol for symbol in candidates if symbol["file"] in goal_files])
        if len(same_file) == 1:
            selected.append(candidate(same_file[0], "same_file"))
            continue
        if same_file:
            continue  # 同文件多定义：不猜测调用目标。
        imported_candidates = unique([symbol for symbol in candidates
                                      if symbol["file"] in imported])
        if len(imported_candidates) == 1:
            selected.append(candidate(imported_candidates[0], "imported"))
            continue
        if imported_candidates:
            continue
        all_candidates = unique(candidates)
        if len(all_candidates) == 1:
            selected.append(candidate(all_candidates[0], "unique_name"))

    selected.sort(key=lambda value: (_RELATION_RANK.get(value["relation"], 9),
                                     value["file_path"], value["start_line"], value["symbol"]))
    output, seen, total = [], set(), 0
    for value in selected:
        key = (value["file_path"], value["symbol"], value["start_line"])
        if key in seen:
            continue
        size = len(value["signature"]) + len(value["docstring"] or "") + len(value["file_path"]) + 80
        if len(output) >= MAX_DEFINITIONS or total + size > MAX_TOTAL_CHARS:
            break
        seen.add(key)
        total += size
        output.append(value)
    return output
