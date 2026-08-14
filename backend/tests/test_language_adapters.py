from pathlib import Path

from app.languages.adapters import TreeSitterLanguageAdapter
from app.languages.base import AnalysisLevel


def test_semantic_parser_failure_downgrades_to_heuristic(tmp_path: Path) -> None:
    source = tmp_path / "module.ts"
    source.write_text(
        "export function load() { return fetchData() }\n",
        encoding="utf-8",
    )

    adapter = TreeSitterLanguageAdapter(
        "typescript",
        {".ts"},
        lambda _: (_ for _ in ()).throw(ValueError("grammar unavailable")),
    )
    analysis = adapter.analyze(source, "module.ts")

    assert analysis.level == AnalysisLevel.heuristic
    assert analysis.parser_id == "regex.typescript.v1"
    assert analysis.symbols[0].name == "load"
    assert analysis.diagnostics and "downgraded to L1" in analysis.diagnostics[0]
