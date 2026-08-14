"""不可动态加载代码的内置语言分析注册表。"""

from __future__ import annotations

from pathlib import Path

from app.languages.adapters import (
    RegexLanguageAdapter,
    TreeSitterLanguageAdapter,
    load_javascript_grammar,
    load_python_grammar,
    load_typescript_grammar,
)
from app.languages.base import AnalysisLevel, FileAnalysis, LanguageAnalysisAdapter


class LanguageAnalysisRegistry:
    def __init__(self, adapters: list[LanguageAnalysisAdapter]) -> None:
        self._by_id = {adapter.language_id: adapter for adapter in adapters}
        self._by_extension = {
            extension: adapter
            for adapter in adapters
            for extension in adapter.extensions
        }

    def detect_language(self, path: str | Path) -> str:
        adapter = self._by_extension.get(Path(path).suffix.casefold())
        return adapter.language_id if adapter is not None else "unknown"

    def get(self, language_id: str) -> LanguageAnalysisAdapter | None:
        return self._by_id.get(language_id)

    def for_path(self, path: str | Path) -> LanguageAnalysisAdapter | None:
        return self._by_extension.get(Path(path).suffix.casefold())

    def analyze(self, file_path: Path, relative_path: str) -> FileAnalysis:
        adapter = self.for_path(relative_path)
        if adapter is None:
            return FileAnalysis(
                language_id="unknown",
                level=AnalysisLevel.text,
                parser_id="text.v1",
            )
        try:
            return adapter.analyze(file_path, relative_path)
        except Exception as exc:
            return FileAnalysis(
                language_id=adapter.language_id,
                level=AnalysisLevel.text,
                parser_id="text.v1",
                diagnostics=[f"analysis failed closed to L0: {type(exc).__name__}"],
            )

    def capability(self, language_id: str) -> dict[str, object]:
        adapter = self.get(language_id)
        if adapter is None:
            return {"language_id": language_id, "max_level": 0, "parser_id": "text.v1"}
        return {
            "language_id": language_id,
            "max_level": int(adapter.max_level),
            "rule_pack_id": adapter.rule_pack_id,
            "extensions": sorted(adapter.extensions),
        }


default_language_registry = LanguageAnalysisRegistry([
    TreeSitterLanguageAdapter("python", {".py"}, load_python_grammar),
    TreeSitterLanguageAdapter(
        "typescript", {".ts", ".tsx", ".mts", ".cts"}, load_typescript_grammar
    ),
    TreeSitterLanguageAdapter(
        "javascript", {".js", ".jsx", ".mjs", ".cjs"}, load_javascript_grammar
    ),
    RegexLanguageAdapter("java", {".java"}),
    RegexLanguageAdapter("go", {".go"}),
    RegexLanguageAdapter("rust", {".rs"}),
])
