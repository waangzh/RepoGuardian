"""语言分析适配器协议与统一的索引产物。"""

from __future__ import annotations

from enum import IntEnum
from pathlib import Path, PurePosixPath
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class AnalysisLevel(IntEnum):
    """上下文分析能力：L0 文本、L1 启发式、L2 Tree-sitter。"""

    text = 0
    heuristic = 1
    semantic = 2


class ImportRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    module: str
    kind: str = "import"
    line: int | None = Field(default=None, ge=1)
    confidence: float = Field(ge=0, le=1)
    parser_id: str


class CallRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    callee: str
    simple_name: str
    line: int | None = Field(default=None, ge=1)
    confidence: float = Field(ge=0, le=1)
    parser_id: str


class SymbolRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str
    name: str
    kind: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    signature: str = ""
    docstring: str | None = None
    calls: list[CallRef] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    parser_id: str
    exported: bool | None = None
    container: str | None = None

    def to_index_entry(self) -> dict[str, object]:
        """保留旧索引字段，同时公开带置信度的统一 CallRef。"""
        return {
            "file": self.file,
            "symbol": self.name,
            "type": self.kind,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "signature": self.signature,
            "docstring": self.docstring,
            "calls": sorted({item.callee for item in self.calls}),
            "call_refs": [item.model_dump(mode="json") for item in self.calls],
            "confidence": self.confidence,
            "parser_id": self.parser_id,
            "exported": self.exported,
            "container": self.container,
        }


class FileAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language_id: str
    level: AnalysisLevel
    parser_id: str
    imports: list[ImportRef] = Field(default_factory=list)
    symbols: list[SymbolRef] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)


class LanguageAnalysisAdapter(Protocol):
    """只描述静态分析能力，不承诺本地工具链或命令执行能力。"""

    language_id: str
    extensions: frozenset[str]
    rule_pack_id: str
    max_level: AnalysisLevel

    def analyze(self, file_path: Path, relative_path: str) -> FileAnalysis:
        """分析一个已通过仓库路径策略校验的文件。"""

    def is_test_path(self, path: str) -> bool:
        """判断路径是否符合该语言的测试文件约定。"""


def default_test_path(path: str) -> bool:
    lowered = path.casefold()
    name = PurePosixPath(lowered).name
    return (
        any(part in {"test", "tests", "testing", "__tests__"} for part in PurePosixPath(lowered).parts)
        or name.startswith("test_")
        or name.endswith(("_test.py", "_test.go"))
        or any(token in name for token in (".test.", ".spec."))
    )
