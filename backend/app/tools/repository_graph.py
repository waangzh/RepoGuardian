"""Repository intelligence graph built from deterministic static indexes."""

from __future__ import annotations

from collections import defaultdict
from pathlib import PurePosixPath
from typing import Any


_SOURCE_EXTENSIONS = {
    "python": (".py",),
    "javascript": (".js", ".jsx", ".ts", ".tsx"),
    "typescript": (".ts", ".tsx", ".js", ".jsx"),
    "java": (".java",),
    "go": (".go",),
    "rust": (".rs",),
}
_CONFIG_NAMES = {
    "pyproject.toml", "setup.cfg", "setup.py", "tox.ini", "pytest.ini",
    "package.json", "tsconfig.json", "vite.config.ts", "vite.config.js",
    "go.mod", "cargo.toml", "pom.xml", "build.gradle", "build.gradle.kts",
}


def build_repository_graph(
    file_index: list[dict[str, Any]], symbol_index: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build a serializable graph without granting any additional file access."""
    files = {str(item["path"]): item for item in file_index if item.get("path")}
    symbols = [item for item in symbol_index if item.get("file") in files]
    edges: list[dict[str, Any]] = []

    for source, item in files.items():
        for imported in item.get("imports", []):
            resolved = resolve_import(source, str(imported), files)
            for target, confidence in resolved:
                edges.append(_edge(
                    "file", source, "file", target, "imports", confidence,
                    f"{source} imports {imported}",
                ))

    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for symbol in symbols:
        name = str(symbol.get("symbol", ""))
        by_name[name].append(symbol)
        by_name[name.rsplit(".", 1)[-1]].append(symbol)
    for caller in symbols:
        for call in caller.get("calls", []):
            simple = str(call).rsplit(".", 1)[-1]
            candidates = {
                (str(item["file"]), str(item["symbol"])): item
                for item in by_name.get(simple, [])
                if item is not caller
            }
            if len(candidates) != 1:
                continue
            callee = next(iter(candidates.values()))
            edges.append(_edge(
                "symbol", _symbol_id(caller), "symbol", _symbol_id(callee),
                "calls", 0.88, f"{caller.get('symbol')} calls {call}",
                source_file=str(caller["file"]), target_file=str(callee["file"]),
            ))

    source_files = [path for path in files if not _is_test(path)]
    for test_path in sorted(path for path in files if _is_test(path)):
        ranked = sorted(
            (
                (_test_match_score(test_path, source), source)
                for source in source_files
            ),
            key=lambda item: (-item[0], item[1]),
        )
        if ranked and ranked[0][0] > 0:
            score, source = ranked[0]
            edges.append(_edge(
                "file", test_path, "file", source, "test_of",
                min(0.98, 0.78 + score * 0.05), f"{test_path} matches tests for {source}",
            ))

    for config in sorted(path for path in files if PurePosixPath(path).name.casefold() in _CONFIG_NAMES):
        config_dir = PurePosixPath(config).parent
        for target in sorted(files):
            if target == config or _is_config(target):
                continue
            target_dir = PurePosixPath(target).parent
            if config_dir == PurePosixPath(".") or config_dir in target_dir.parents or config_dir == target_dir:
                edges.append(_edge(
                    "file", config, "file", target, "configures", 0.72,
                    f"{config} configures files in its directory tree",
                ))

    return {
        "version": 1,
        "files": [
            {
                "id": path,
                "path": path,
                "language": item.get("language", "unknown"),
                "size": item.get("size", 0),
            }
            for path, item in sorted(files.items())
        ],
        "symbols": [
            {
                "id": _symbol_id(item),
                "file": item.get("file"),
                "name": item.get("symbol"),
                "kind": item.get("type"),
                "start_line": item.get("start_line"),
                "end_line": item.get("end_line"),
            }
            for item in symbols
        ],
        "edges": _dedupe_edges(edges),
        "metadata": {
            "file_count": len(files),
            "symbol_count": len(symbols),
            "edge_count": len(_dedupe_edges(edges)),
            "supported_edges": ["imports", "calls", "test_of", "configures"],
        },
    }


def resolve_import(
    source: str, imported: str, files: dict[str, dict[str, Any]]
) -> list[tuple[str, float]]:
    """Resolve a language import to repository files, preferring exact paths."""
    language = str((files.get(source) or {}).get("language", "unknown"))
    module = imported.strip().strip("'\"")
    if not module:
        return []
    candidates: list[tuple[str, float]] = []
    source_path = PurePosixPath(source)

    if language == "python":
        level = len(module) - len(module.lstrip("."))
        dotted = module.lstrip(".")
        if level:
            base = list(source_path.parent.parts)
            base = base[:max(0, len(base) - level + 1)]
            parts = [*base, *dotted.split(".")] if dotted else base
        else:
            parts = dotted.split(".")
        candidate_bases = ["/".join(parts)]
        if not level:
            candidate_bases.extend(
                "/".join((*prefix, *parts))
                for prefix in (("src",), ("backend",), ("backend", "src"))
            )
        for base in candidate_bases:
            for path in (f"{base}.py", f"{base}/__init__.py"):
                if path in files:
                    candidates.append((path, 0.99 if base == candidate_bases[0] else 0.94))
    elif language in {"javascript", "typescript"} and module.startswith("."):
        base = (source_path.parent / module).as_posix()
        for ext in _SOURCE_EXTENSIONS[language]:
            for path in (f"{base}{ext}", f"{base}/index{ext}"):
                if path in files:
                    candidates.append((path, 0.99))
    elif language == "java":
        suffix = module.replace(".", "/") + ".java"
        for path in files:
            if path == suffix or path.endswith("/" + suffix):
                candidates.append((path, 0.96))
    elif language == "go":
        suffix = module.rstrip("/")
        for path, item in files.items():
            if item.get("language") == "go" and (
                PurePosixPath(path).parent.as_posix() == suffix
                or suffix.endswith("/" + PurePosixPath(path).parent.as_posix())
            ):
                candidates.append((path, 0.9))
    elif language == "rust":
        parts = [part for part in module.replace("::", "/").split("/") if part]
        if parts and parts[0] in {"crate", "self", "super"}:
            parts = parts[1:]
        for length in range(len(parts), 0, -1):
            base = "/".join(parts[:length])
            for prefix in (source_path.parent.as_posix(), "src"):
                for path in (f"{prefix}/{base}.rs", f"{prefix}/{base}/mod.rs"):
                    if path in files:
                        candidates.append((path, 0.94))
            if candidates:
                break

    if not candidates:
        stem = module.replace("::", ".").rsplit(".", 1)[-1].rsplit("/", 1)[-1]
        matches = [path for path in files if PurePosixPath(path).stem == stem]
        if len(matches) == 1:
            candidates.append((matches[0], 0.68))
    return sorted(set(candidates), key=lambda item: (-item[1], item[0]))


def _edge(
    source_kind: str, source: str, target_kind: str, target: str,
    kind: str, confidence: float, why: str, **extra: Any,
) -> dict[str, Any]:
    return {
        "source_kind": source_kind, "source": source,
        "target_kind": target_kind, "target": target,
        "type": kind, "confidence": confidence, "why": why, **extra,
    }


def _symbol_id(symbol: dict[str, Any]) -> str:
    return f"{symbol.get('file')}::{symbol.get('symbol')}"


def _is_test(path: str) -> bool:
    lowered = path.casefold()
    name = PurePosixPath(lowered).name
    return (
        any(part in {"test", "tests", "testing", "__tests__"} for part in PurePosixPath(lowered).parts)
        or name.startswith("test_") or name.endswith("_test.py")
        or any(token in name for token in (".test.", ".spec."))
    )


def _is_config(path: str) -> bool:
    return PurePosixPath(path).name.casefold() in _CONFIG_NAMES


def _test_match_score(test_path: str, source_path: str) -> int:
    test_stem = PurePosixPath(test_path).stem.casefold()
    source_stem = PurePosixPath(source_path).stem.casefold()
    normalized = test_stem.removeprefix("test_").removesuffix("_test")
    for suffix in (".test", ".spec"):
        normalized = normalized.removesuffix(suffix)
    score = 4 if normalized == source_stem else 0
    if source_stem in test_stem:
        score += 2
    if set(PurePosixPath(test_path).parts[:-1]) & set(PurePosixPath(source_path).parts[:-1]):
        score += 1
    return score


def _dedupe_edges(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[tuple[str, str, str], dict[str, Any]] = {}
    for edge in edges:
        key = (str(edge["source"]), str(edge["target"]), str(edge["type"]))
        if key not in best or float(edge["confidence"]) > float(best[key]["confidence"]):
            best[key] = edge
    return sorted(best.values(), key=lambda edge: (
        str(edge["type"]), str(edge["source"]), str(edge["target"])
    ))
