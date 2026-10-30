"""Run-local inventory and query-driven use of the existing isolated parsers."""

import asyncio
import threading
from pathlib import Path

from app.languages.isolation import analyze_isolated
from app.languages.registry import default_language_registry
from app.tools.repo_indexer import _scan_imports
from app.tools.repository_graph import (
    build_repository_graph, resolved_import_targets, reverse_import_hints,
)


class OnDemandIndex:
    def __init__(self, evidence):
        self.evidence = evidence
        self.inventory = tuple(sorted(evidence.allowed_paths))
        self.entries = {path: {"path": path,
            "language": default_language_registry.detect_language(path), "imports": [],
            "import_refs": [], "parser_id": "unparsed"} for path in self.inventory}
        self.analyses, self.scanned = {}, {}
        self.cancel_event = threading.Event()
        self._lock = asyncio.Lock()

    def _parse(self, path, side):
        if path not in self.entries:
            raise ValueError("path_not_authorized")
        key = (side, path)
        if key not in self.analyses:
            _, content = self.evidence._source(path, side)
            adapter = default_language_registry.for_path(path)
            if adapter is None:
                raise ValueError("parser_unavailable")
            self.analyses[key] = analyze_isolated(adapter, content.encode("utf-8"), path,
                Path(path).suffix.casefold(), cancel_event=self.cancel_event)
        return self.analyses[key]

    def _head_entries(self):
        entries = {path: dict(item) for path, item in self.entries.items()}
        for (side, path), analysis in self.analyses.items():
            if side == "head":
                entries[path].update(imports=sorted({ref.module for ref in analysis.imports}),
                    import_refs=[ref.model_dump() for ref in analysis.imports],
                    python_namespace=analysis.python_namespace, parser_id=analysis.parser_id)
        return entries

    async def symbol(self, path, name, side="head"):
        async with self._lock:
            analysis = await asyncio.to_thread(self._parse, path, side)
            matches = [symbol.to_index_entry() for symbol in analysis.symbols
                       if name in {symbol.name, symbol.symbol_id}]
            if len(matches) != 1:
                raise ValueError("symbol_missing_or_ambiguous")
            return matches[0]

    def _relations(self, path, name, direction, scope):
        analysis = self._parse(path, "head")
        matches = [symbol.to_index_entry() for symbol in analysis.symbols
                   if name in {symbol.name, symbol.symbol_id}]
        if len(matches) != 1:
            raise ValueError("symbol_missing_or_ambiguous")
        entries = self._head_entries()
        targets = resolved_import_targets(list(entries.values()), {path})
        if direction in {"in", "both"}:
            remaining, count = 8 * 1024 * 1024, 0
            for candidate in self.inventory:
                if count >= 512 or remaining <= 0:
                    break
                if (candidate == path or entries[candidate]["language"] == "unknown"
                        or (scope and not candidate.startswith(scope.rstrip("/") + "/") and candidate != scope)):
                    continue
                count += 1
                if candidate not in self.scanned:
                    try:
                        _, content = self.evidence._source(candidate, "head")
                    except (ValueError, OSError):
                        continue
                    encoded = content.encode("utf-8")
                    remaining -= len(encoded)
                    if remaining < 0:
                        break
                    self.scanned[candidate] = _scan_imports(Path(candidate),
                        entries[candidate]["language"], source=encoded, cancel_event=self.cancel_event)
            hints = reverse_import_hints(list(entries.values()), self.scanned, {path})
            targets.update(hints)
        for target in sorted(targets - {path})[:32]:
            try:
                self._parse(target, "head")
            except (ValueError, OSError):
                continue
        symbols = [symbol.to_index_entry() for (side, _), result in self.analyses.items()
                   if side == "head" for symbol in result.symbols]
        graph = build_repository_graph(list(self._head_entries().values()), symbols)
        selected = matches[0].get("symbol_id") or f"{path}::{matches[0]['symbol']}"
        edges = [edge for edge in graph["edges"] if edge["type"] == "calls" and (
            (direction in {"in", "both"} and edge["target"] == selected)
            or (direction in {"out", "both"} and edge["source"] == selected))]
        return {"ok": True, "source": "static_relation_hint", "edges": edges[:40],
            "truncated": len(edges) > 40, "scope_complete": False,
            "parsed_files": len(self.analyses), "scanned_files": len(self.scanned),
            "limitation": "Bounded static relation hints, not runtime binding or exhaustive absence."}

    async def relations(self, path, name, direction, scope=""):
        async with self._lock:
            return await asyncio.to_thread(self._relations, path, name, direction, scope)
