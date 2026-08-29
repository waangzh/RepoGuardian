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
    lookup = _build_lookup(files)
    edges: list[dict[str, Any]] = []

    for source, item in files.items():
        import_refs = item.get("import_refs") or [
            {"module": imported, "confidence": 1.0, "parser_id": "legacy"}
            for imported in item.get("imports", [])
        ]
        for import_ref in import_refs:
            imported = str(import_ref.get("module", ""))
            resolved = resolve_import(source, imported, files, lookup)
            for target, confidence in resolved:
                edges.append(_edge(
                    "file", source, "file", target, "imports",
                    min(confidence, float(import_ref.get("confidence", confidence))),
                    f"{source} imports {imported}",
                    parser_id=import_ref.get("parser_id"),
                ))

    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for symbol in symbols:
        name = str(symbol.get("symbol", ""))
        by_name[name].append(symbol)
        by_name[name.rsplit(".", 1)[-1]].append(symbol)
    for caller in symbols:
        call_refs = caller.get("call_refs") or [
            {"callee": call, "simple_name": str(call).rsplit(".", 1)[-1], "confidence": 0.88}
            for call in caller.get("calls", [])
        ]
        for call_ref in call_refs:
            call = str(call_ref.get("callee", ""))
            simple = str(call_ref.get("simple_name") or call.rsplit(".", 1)[-1])
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
                "calls", min(0.92, float(call_ref.get("confidence", 0.7))),
                f"{caller.get('symbol')} calls {call}",
                source_file=str(caller["file"]), target_file=str(callee["file"]),
                parser_id=call_ref.get("parser_id"),
            ))

    for test_path in sorted(lookup["test_files"]):
        candidates = _candidate_sources_for_test(test_path, lookup)
        ranked = sorted(
            (
                (_test_match_score(test_path, source), source)
                for source in candidates
            ),
            key=lambda item: (-item[0], item[1]),
        )
        if ranked and ranked[0][0] > 0:
            score, source = ranked[0]
            edges.append(_edge(
                "file", test_path, "file", source, "test_of",
                min(0.98, 0.78 + score * 0.05), f"{test_path} matches tests for {source}",
            ))

    for config, targets in sorted(lookup["config_targets"].items()):
        for target in targets:
            edges.append(_edge(
                "file", config, "file", target, "configures", 0.72,
                f"{config} configures files in its directory tree",
            ))

    deduped_edges = _dedupe_edges(edges)
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
                "confidence": item.get("confidence"),
                "parser_id": item.get("parser_id"),
            }
            for item in symbols
        ],
        "edges": deduped_edges,
        "metadata": {
            "file_count": len(files),
            "symbol_count": len(symbols),
            "edge_count": len(deduped_edges),
            "supported_edges": ["imports", "calls", "test_of", "configures"],
        },
    }


def resolve_import(
    source: str,
    imported: str,
    files: dict[str, dict[str, Any]],
    lookup: dict[str, Any] | None = None,
) -> list[tuple[str, float]]:
    """Resolve a language import to repository files, preferring exact paths."""
    language = str((files.get(source) or {}).get("language", "unknown"))
    module = imported.strip().strip("'\"")
    if not module:
        return []
    if lookup is None:
        lookup = _build_lookup(files)
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
        candidate_modules = [".".join(part for part in parts if part)]
        if not level and candidate_modules[0]:
            candidate_modules.extend(
                ".".join((*prefix, *parts))
                for prefix in (("src",), ("backend",), ("backend", "src"))
            )
        for index, candidate in enumerate(candidate_modules):
            for path in lookup["python_modules"].get(candidate, ()):
                candidates.append((path, 0.99 if index == 0 else 0.94))
    elif language in {"javascript", "typescript"} and module.startswith("."):
        base = (source_path.parent / module).as_posix()
        for ext in _SOURCE_EXTENSIONS[language]:
            for path in (f"{base}{ext}", f"{base}/index{ext}"):
                if path in files:
                    candidates.append((path, 0.99))
    elif language == "java":
        for path in lookup["java_modules"].get(module, ()):
            candidates.append((path, 0.96))
    elif language == "go":
        suffix = module.rstrip("/")
        for path in lookup["go_directories"].get(suffix, ()):
            candidates.append((path, 0.9))
        for directory, paths in lookup["go_directories"].items():
            if suffix.endswith("/" + directory):
                candidates.extend((path, 0.9) for path in paths)
    elif language == "rust":
        parts = [part for part in module.replace("::", "/").split("/") if part]
        if parts and parts[0] in {"crate", "self", "super"}:
            parts = parts[1:]
        for length in range(len(parts), 0, -1):
            base = "/".join(parts[:length])
            for prefix in (source_path.parent.as_posix(), "src"):
                key = f"{prefix}/{base}".strip("/")
                for path in lookup["rust_modules"].get(key, ()):
                    candidates.append((path, 0.94))
            if candidates:
                break

    if not candidates:
        stem = module.replace("::", ".").rsplit(".", 1)[-1].rsplit("/", 1)[-1]
        matches = lookup["stem_to_paths"].get(stem, ())
        if len(matches) == 1:
            candidates.append((matches[0], 0.68))
    return sorted(set(candidates), key=lambda item: (-item[1], item[0]))


def _build_lookup(files: dict[str, dict[str, Any]]) -> dict[str, Any]:
    stem_to_paths: dict[str, list[str]] = defaultdict(list)
    python_modules: dict[str, list[str]] = defaultdict(list)
    java_modules: dict[str, list[str]] = defaultdict(list)
    go_directories: dict[str, list[str]] = defaultdict(list)
    rust_modules: dict[str, list[str]] = defaultdict(list)
    test_files: list[str] = []
    source_stem_index: dict[str, list[str]] = defaultdict(list)
    source_stems: dict[str, str] = {}
    source_directories: dict[str, frozenset[str]] = {}
    sources_by_directory: dict[str, set[str]] = defaultdict(set)
    sources_by_trigram: dict[str, set[str]] = defaultdict(set)
    short_stem_sources: set[str] = set()
    config_targets: dict[str, list[str]] = defaultdict(list)
    targets_by_directory: dict[str, list[str]] = defaultdict(list)

    for path, item in files.items():
        path_obj = PurePosixPath(path)
        stem_to_paths[path_obj.stem].append(path)
        language = str(item.get("language", "unknown"))

        if _is_test(path):
            test_files.append(path)
        else:
            source_stem = path_obj.stem.casefold()
            source_stem_index[source_stem].append(path)
            source_stems[path] = source_stem
            directory_parts = frozenset(path_obj.parts[:-1])
            source_directories[path] = directory_parts
            for part in directory_parts:
                sources_by_directory[part].add(path)
            if len(source_stem) < 3:
                short_stem_sources.add(path)
            else:
                for trigram in _trigrams(source_stem):
                    sources_by_trigram[trigram].add(path)

        if language == "python":
            for module in _python_module_candidates(path_obj):
                python_modules[module].append(path)
        elif language == "java":
            module_parts = list(path_obj.with_suffix("").parts)
            for index in range(len(module_parts)):
                java_modules[".".join(module_parts[index:])].append(path)
        elif language == "go":
            go_directories[path_obj.parent.as_posix()].append(path)
        elif language == "rust":
            for module in _rust_module_candidates(path_obj):
                rust_modules[module].append(path)

        if path != "." and not _is_config(path):
            for directory in _ancestor_directories(path_obj.parent):
                targets_by_directory[directory].append(path)

    for path in files:
        if not _is_config(path):
            continue
        config_dir = PurePosixPath(path).parent.as_posix()
        config_targets[path] = sorted(
            target for target in targets_by_directory.get(config_dir, []) if target != path
        )

    return {
        "stem_to_paths": {key: sorted(values) for key, values in stem_to_paths.items()},
        "python_modules": {key: sorted(values) for key, values in python_modules.items()},
        "java_modules": {key: sorted(values) for key, values in java_modules.items()},
        "go_directories": {key: sorted(values) for key, values in go_directories.items()},
        "rust_modules": {key: sorted(values) for key, values in rust_modules.items()},
        "test_files": sorted(test_files),
        "source_stem_index": {key: sorted(values) for key, values in source_stem_index.items()},
        "source_stems": source_stems,
        "source_directories": source_directories,
        "sources_by_directory": {
            key: sorted(values) for key, values in sources_by_directory.items()
        },
        "sources_by_trigram": {
            key: sorted(values) for key, values in sources_by_trigram.items()
        },
        "short_stem_sources": sorted(short_stem_sources),
        "config_targets": dict(sorted(config_targets.items())),
    }


def _python_module_candidates(path: PurePosixPath) -> list[str]:
    parts = list(path.parts)
    if path.name == "__init__.py":
        module_parts = parts[:-1]
    else:
        module_parts = [*parts[:-1], path.stem]
    candidates: set[tuple[str, ...]] = {tuple(module_parts)}
    if len(module_parts) >= 1 and module_parts[0] in {"src", "backend"}:
        candidates.add(tuple(module_parts[1:]))
    if len(module_parts) >= 2 and module_parts[:2] == ["backend", "src"]:
        candidates.add(tuple(module_parts[2:]))
    return sorted(".".join(item for item in candidate if item) for candidate in candidates if candidate)


def _rust_module_candidates(path: PurePosixPath) -> list[str]:
    candidates = []
    if path.name == "mod.rs":
        candidates.append(path.parent.as_posix())
    else:
        candidates.append((path.parent / path.stem).as_posix())
    return sorted(set(item.strip("/") for item in candidates if item))


def _ancestor_directories(path: PurePosixPath) -> list[str]:
    directories = [path.as_posix()]
    directories.extend(parent.as_posix() for parent in path.parents)
    return [item for item in dict.fromkeys(directories) if item]


def _candidate_sources_for_test(test_path: str, lookup: dict[str, Any]) -> list[str]:
    test_path_obj = PurePosixPath(test_path)
    test_stem = test_path_obj.stem.casefold()
    normalized = _normalize_test_stem(test_stem)
    exact = lookup["source_stem_index"].get(normalized, ())
    if exact:
        return list(exact)

    candidates: set[str] = set()
    for trigram in _trigrams(test_stem):
        candidates.update(lookup["sources_by_trigram"].get(trigram, ()))
    candidates.update(lookup["short_stem_sources"])
    for directory in test_path_obj.parts[:-1]:
        candidates.update(lookup["sources_by_directory"].get(directory, ()))

    source_stems = lookup["source_stems"]
    source_directories = lookup["source_directories"]
    test_directories = frozenset(test_path_obj.parts[:-1])
    return sorted(
        source
        for source in candidates
        if (
            source_stems[source] in test_stem
            or bool(test_directories & source_directories[source])
        )
    )


def _trigrams(value: str) -> set[str]:
    if len(value) < 3:
        return set()
    return {value[index:index + 3] for index in range(len(value) - 2)}


def _normalize_test_stem(stem: str) -> str:
    normalized = stem.removeprefix("test_").removesuffix("_test")
    for suffix in (".test", ".spec"):
        normalized = normalized.removesuffix(suffix)
    return normalized


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
    normalized = _normalize_test_stem(test_stem)
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
