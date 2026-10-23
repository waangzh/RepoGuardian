"""按仓库相对路径匹配确定性规划使用的语言。"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Iterable


_EXTENSION_LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
}

def language_for_path(
    path: str,
    file_index: Iterable[dict[str, Any]] | None = None,
) -> str:
    """优先使用索引语言，缺失时按扩展名确定性回退。"""
    normalized = PurePosixPath(path).as_posix()
    for item in file_index or ():
        if str(item.get("path", "")) == normalized:
            language = str(item.get("language", "unknown"))
            if language != "unknown":
                return language
            break
    return _EXTENSION_LANGUAGES.get(PurePosixPath(normalized).suffix.casefold(), "unknown")
