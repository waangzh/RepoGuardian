"""仓库索引器 —— 扫描仓库构建 Repository Intelligence 索引。

产出：
    1. file_index   — [{path, language, size, imports}, ...]
    2. symbol_index — 多语言声明、签名与保守调用关系
    3. repository_graph — 文件、符号和 imports/calls/test/configures 边
    4. project_meta — 多语言、框架、测试目录、入口点
"""

import ast
import asyncio
import os
import re
import threading
from pathlib import Path
from typing import Any

from app.languages.registry import default_language_registry
from app.languages.base import AnalysisLevel, FileAnalysis
from app.languages.isolation import ParserCancelled
from app.tools.base import BaseTool
from app.review.tool_scope import ReviewPathPolicyError, validate_repository_file
from app.tools.git_tool import GitTool
from app.tools.repository_graph import build_repository_graph

# 扫描时跳过的目录和文件
_IGNORED_DIRS = frozenset({
    ".git", "venv", "node_modules", "dist", "build",
    "__pycache__", ".pytest_cache", ".coverage", ".mypy_cache",
    ".ruff_cache", ".tox", ".eggs", ".repoguardian",
})

_IGNORED_FILES = frozenset({".DS_Store", "Thumbs.db"})

# 项目配置文件（用于 detect_project_meta）
_PROJECT_CONFIG_FILES = {
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "makefile",
    "tox.ini", "pipfile", "pytest.ini", "package.json", "tsconfig.json",
    "vite.config.ts", "vite.config.js", "go.mod", "cargo.toml", "pom.xml",
    "build.gradle", "build.gradle.kts",
}

# 框架检测：import 关键字 → 框架名
_FRAMEWORK_HINTS = {
    "fastapi": {"fastapi", "starlette"},
    "flask": {"flask"},
    "django": {"django"},
    "sqlalchemy": {"sqlalchemy"},
    "react": {"react"},
    "vue": {"vue"},
    "nextjs": {"next"},
    "express": {"express"},
    "spring": {"org.springframework"},
    "gin": {"github.com/gin-gonic/gin"},
    "axum": {"axum"},
    "actix-web": {"actix_web"},
}


class RepoIndexer(BaseTool):
    """仓库结构扫描器，构建文件级和符号级索引。"""
    name = "repo_indexer"
    description = "Scan repository structure and build file-level and symbol-level index."

    def __init__(self):
        self._cancel_event = threading.Event()
        self._symbol_index: list[dict[str, Any]] | None = None

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        self._cancel_event = kwargs.get("cancel_event") or threading.Event()
        self._symbol_index = []
        try:
            return await self._execute(**kwargs)
        finally:
            self._symbol_index = None

    async def _execute(self, **kwargs: Any) -> dict[str, Any]:
        """执行完整扫描，返回三层索引。"""
        repo_path = kwargs["repo_path"]
        progress = kwargs.get("progress")
        if progress:
            await asyncio.to_thread(progress, "index_files", "正在建立文件索引")
        file_index = await self.build_file_index(repo_path)
        if progress:
            await asyncio.to_thread(progress, "index_symbols", "正在建立符号索引")
        symbol_index = await self.build_symbol_index(repo_path)
        if progress:
            await asyncio.to_thread(progress, "index_relationships", "正在建立调用关系图")
        repository_graph = await asyncio.to_thread(
            build_repository_graph, file_index, symbol_index
        )
        project_meta = await self.detect_project_meta(repo_path, file_index)
        return {
            "file_index": file_index,
            "symbol_index": symbol_index,
            "repository_graph": repository_graph,
            "project_meta": project_meta,
        }

    async def build_file_index(self, repo_path: str) -> list[dict[str, Any]]:
        try:
            return await asyncio.to_thread(self._build_file_index_sync, repo_path)
        except asyncio.CancelledError:
            self._cancel_event.set()
            raise

    def _analyze(self, file_path: Path, relative_path: str) -> FileAnalysis:
        if self._cancel_event.is_set():
            raise ParserCancelled()
        return default_language_registry.analyze(
            file_path, relative_path, cancel_event=self._cancel_event)

    def _build_file_index_sync(self, repo_path: str) -> list[dict[str, Any]]:
        """遍历仓库目录，构建文件级索引（路径、语言、大小、导入）。"""
        index: list[dict[str, Any]] = []
        root = Path(repo_path)
        tracked_files = GitTool().list_tracked_files(root)
        enforce_tracked = (root / ".git").exists()
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = Path(dirpath).relative_to(root)
            # 跳过忽略目录
            if rel_dir.parts and rel_dir.parts[0] in _IGNORED_DIRS:
                dirnames[:] = []
                continue
            dirnames[:] = sorted(d for d in dirnames if d not in _IGNORED_DIRS)
            for filename in sorted(filenames):
                if self._cancel_event.is_set():
                    raise ParserCancelled()
                if filename in _IGNORED_FILES:
                    continue
                file_path_obj = Path(dirpath) / filename
                rel_path = file_path_obj.relative_to(root).as_posix()
                if enforce_tracked and rel_path not in tracked_files:
                    continue
                try:
                    validate_repository_file(root, rel_path, tracked_files=tracked_files)
                except (OSError, ReviewPathPolicyError):
                    continue
                try:
                    stat = file_path_obj.stat()
                    size = stat.st_size
                except OSError:
                    size = 0
                analysis = (
                    self._analyze(file_path_obj, rel_path)
                    if _detect_language(filename) != "unknown"
                    else FileAnalysis(language_id="unknown", level=AnalysisLevel.text,
                                      parser_id="text.v1")
                )
                if self._symbol_index is not None:
                    self._symbol_index.extend(symbol.to_index_entry() for symbol in analysis.symbols)
                language = analysis.language_id
                index.append({
                    "path": rel_path,
                    "language": language,
                    "size": size,
                    "imports": sorted({item.module for item in analysis.imports}),
                    "import_refs": [item.model_dump(mode="json") for item in analysis.imports],
                    "python_namespace": analysis.python_namespace,
                    "analysis_level": int(analysis.level),
                    "parser_id": analysis.parser_id,
                    "diagnostics": analysis.diagnostics,
                })
        return sorted(index, key=lambda f: f["path"])

    async def build_symbol_index(self, repo_path: str) -> list[dict[str, Any]]:
        if self._symbol_index is not None:
            return self._symbol_index
        try:
            return await asyncio.to_thread(self._build_symbol_index_sync, repo_path)
        except asyncio.CancelledError:
            self._cancel_event.set()
            raise

    def _build_symbol_index_sync(self, repo_path: str) -> list[dict[str, Any]]:
        """通过内置语言适配器提取声明与调用，保留解析器和置信度。"""
        index: list[dict[str, Any]] = []
        root = Path(repo_path)
        tracked_files = GitTool().list_tracked_files(root)
        enforce_tracked = (root / ".git").exists()
        for dirpath, _, filenames in os.walk(root):
            rel_dir = Path(dirpath).relative_to(root)
            if rel_dir.parts and rel_dir.parts[0] in _IGNORED_DIRS:
                continue
            for filename in filenames:
                language = _detect_language(filename)
                if language == "unknown":
                    continue
                file_path_obj = Path(dirpath) / filename
                rel_path = file_path_obj.relative_to(root).as_posix()
                if enforce_tracked and rel_path not in tracked_files:
                    continue
                try:
                    validate_repository_file(root, rel_path, tracked_files=tracked_files)
                except (OSError, ReviewPathPolicyError):
                    continue
                analysis = self._analyze(file_path_obj, rel_path)
                index.extend(symbol.to_index_entry() for symbol in analysis.symbols)
        return index

    async def detect_project_meta(
        self, repo_path: str, file_index: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """检测项目元信息：语言、框架、测试目录、入口点、配置文件。"""
        if file_index is None:
            file_index = await self.build_file_index(repo_path)
        language_counts: dict[str, int] = {}
        for item in file_index:
            detected = str(item.get("language", "unknown"))
            if detected != "unknown":
                language_counts[detected] = language_counts.get(detected, 0) + 1
        languages = sorted(language_counts, key=lambda item: (-language_counts[item], item))
        language = languages[0] if languages else "unknown"

        framework = _detect_framework(repo_path, file_index)
        test_dirs = _find_test_dirs(repo_path)
        config_files = sorted(
            str(item["path"])
            for item in file_index
            if Path(str(item.get("path", ""))).name.casefold() in _PROJECT_CONFIG_FILES
        )

        indexed_paths = {str(item.get("path")) for item in file_index}
        entry_points = [
            entry for entry in (
                "app/main.py", "main.py", "src/main.py", "run.py",
                "src/main.ts", "src/main.js", "src/index.ts", "src/index.js",
                "cmd/main.go", "main.go", "src/main.rs",
                "src/main/java/Application.java",
            ) if entry in indexed_paths
        ]

        return {
            "language": language,
            "languages": languages,
            "language_counts": language_counts,
            "is_mixed_language": len(languages) > 1,
            "framework": framework,
            "test_framework": _detect_test_framework(file_index, test_dirs),
            "entry_points": entry_points,
            "test_dirs": test_dirs,
            "config_files": config_files,
            "total_files": len(file_index),
            "analysis_capabilities": [
                default_language_registry.capability(language_id)
                for language_id in languages
            ],
        }


def _detect_language(filename: str) -> str:
    """根据文件扩展名检测编程语言。"""
    return default_language_registry.detect_language(filename)


def _extract_imports(file_path: Path, language: str = "python") -> list[str]:
    """提取完整导入标识；解析失败时返回空列表而不影响索引。"""
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    imports: list[str] = []
    if language == "python":
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                prefix = "." * node.level
                if node.module:
                    imports.append(prefix + node.module)
                elif prefix:
                    imports.append(prefix)
    elif language in {"javascript", "typescript"}:
        patterns = (
            r"\b(?:import|export)\s+(?:[^;]*?\s+from\s+)?[\"']([^\"']+)[\"']",
            r"\brequire\s*\(\s*[\"']([^\"']+)[\"']\s*\)",
            r"\bimport\s*\(\s*[\"']([^\"']+)[\"']\s*\)",
        )
        for pattern in patterns:
            imports.extend(re.findall(pattern, content))
    elif language == "java":
        imports.extend(re.findall(r"^\s*import\s+(?:static\s+)?([\w.]+)(?:\.\*)?\s*;", content, re.MULTILINE))
    elif language == "go":
        imports.extend(re.findall(r"\bimport\s+(?:[\w.]+\s+)?\"([^\"]+)\"", content))
        for block in re.findall(r"\bimport\s*\((.*?)\)", content, re.DOTALL):
            imports.extend(re.findall(r"(?:^|\n)\s*(?:[\w.]+\s+)?\"([^\"]+)\"", block))
    elif language == "rust":
        imports.extend(re.findall(r"^\s*use\s+([^;{]+)", content, re.MULTILINE))
        imports.extend(re.findall(r"^\s*mod\s+([A-Za-z_][A-Za-z0-9_]*)\s*;", content, re.MULTILINE))
    return sorted(set(imports))


def _detect_framework(
    repo_path: str, file_index: list[dict[str, Any]] | None = None
) -> str | None:
    """通过扫描所有 .py 文件的 import 语句检测使用的 Web 框架。"""
    all_imports: set[str] = set()
    if file_index is not None:
        all_imports = {
            str(imported).lower()
            for item in file_index
            for imported in item.get("imports", [])
        }
        for framework, hints in _FRAMEWORK_HINTS.items():
            if any(_import_matches_hint(imported, hint) for imported in all_imports for hint in hints):
                return framework
        return None
    root = Path(repo_path)
    for dirpath, _, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        if rel.parts and rel.parts[0] in _IGNORED_DIRS:
            continue
        for fname in filenames:
            if fname.endswith(".py"):
                for imp in _extract_imports(Path(dirpath) / fname, "python"):
                    all_imports.add(imp.lower())

    for framework, hints in _FRAMEWORK_HINTS.items():
        if any(_import_matches_hint(imported, hint) for imported in all_imports for hint in hints):
            return framework
    return None


def _import_matches_hint(imported: str, hint: str) -> bool:
    normalized = imported.casefold().lstrip(".")
    expected = hint.casefold()
    return (
        normalized == expected
        or normalized.startswith(expected + ".")
        or normalized.startswith(expected + "/")
        or normalized.startswith(expected + "::")
    )


def _detect_test_framework(
    file_index: list[dict[str, Any]], test_dirs: list[str] | None = None
) -> str | None:
    imports = {
        str(imported).casefold()
        for item in file_index
        for imported in item.get("imports", [])
    }
    paths = {str(item.get("path", "")).casefold() for item in file_index}
    languages = {str(item.get("language", "")) for item in file_index}
    if any(_import_matches_hint(item, "pytest") for item in imports) or (
        "python" in languages and (
            bool(test_dirs)
            or any(Path(path).name.startswith("test_") or "/tests/" in f"/{path}" for path in paths)
        )
    ):
        return "pytest"
    if any(_import_matches_hint(item, "vitest") for item in imports):
        return "vitest"
    if any(_import_matches_hint(item, "jest") or item.startswith("@jest/") for item in imports):
        return "jest"
    if any(_import_matches_hint(item, "org.junit") for item in imports):
        return "junit"
    if "go" in languages and any(path.endswith("_test.go") for path in paths):
        return "go test"
    if "rust" in languages and "cargo.toml" in paths:
        return "cargo test"
    return None


def _find_test_dirs(repo_path: str) -> list[str]:
    """检测仓库根目录下是否存在测试目录。"""
    root = Path(repo_path)
    test_dirs: list[str] = []
    for candidate in ["tests", "test", "testing"]:
        if (root / candidate).is_dir():
            test_dirs.append(candidate)
    return test_dirs
