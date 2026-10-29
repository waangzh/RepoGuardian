"""仓库索引器 —— 扫描仓库构建 Repository Intelligence 索引。

产出：
    1. file_index   — [{path, language, size, imports}, ...]
    2. symbol_index — 多语言声明、签名与保守调用关系
    3. repository_graph — 文件、符号和 imports/calls/test/configures 边
    4. project_meta — 多语言、框架、测试目录、入口点
"""

import asyncio
import hashlib
import io
import json
import os
import re
import threading
import tokenize
from pathlib import Path
from typing import Any

from app.languages.registry import default_language_registry
from app.languages.base import AnalysisLevel, FileAnalysis
from app.languages.isolation import MAX_SOURCE_BYTES, ParserCancelled, parser_fingerprint
from app.tools.base import BaseTool
from app.review.tool_scope import (
    ReviewPathPolicyError, git_worktree_matches_index, list_git_tracked_blobs,
    validate_repository_file,
)
from app.tools.git_tool import GitTool
from app.tools.repository_graph import (
    build_repository_graph, related_test_paths, resolved_import_targets,
    reverse_import_hints,
)

# 扫描时跳过的目录和文件
_IGNORED_DIRS = frozenset({
    ".git", "venv", "node_modules", "dist", "build",
    "__pycache__", ".pytest_cache", ".coverage", ".mypy_cache",
    ".ruff_cache", ".tox", ".eggs", ".repoguardian",
})

_IGNORED_FILES = frozenset({".DS_Store", "Thumbs.db"})

# 索引产物缓存的语义版本；索引字段或解析输入含义变化时递增。
ANALYSIS_CACHE_PROTOCOL = "file-analysis-v2"

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
        """执行扫描，返回三层索引；changed_paths 限定实际解析范围。"""
        repo_path = kwargs["repo_path"]
        changed_paths = kwargs.get("changed_paths")
        progress = kwargs.get("progress")
        if progress:
            await asyncio.to_thread(progress, "index_files", "正在建立文件索引")
        file_index = (await self.build_file_index(repo_path) if changed_paths is None
                      else await self.build_file_index(
                          repo_path, changed_paths=frozenset(changed_paths)))
        if progress:
            await asyncio.to_thread(progress, "index_symbols", "正在建立符号索引")
        symbol_index = await self.build_symbol_index(repo_path)
        if progress:
            await asyncio.to_thread(progress, "index_relationships", "正在建立调用关系图")
        repository_graph = await asyncio.to_thread(
            build_repository_graph, file_index, symbol_index
        )
        code_files = [item for item in file_index if item["language"] != "unknown"]
        unparsed = sum(item["parser_id"] == "unparsed" for item in code_files)
        degraded = sum(item["parser_id"] != "unparsed"
                       and item["analysis_level"] < int(AnalysisLevel.semantic)
                       for item in code_files)
        repository_graph["metadata"].update(
            scope_mode="full" if changed_paths is None else "scoped",
            indexed_file_count=len(code_files) - unparsed,
            unparsed_file_count=unparsed, degraded_file_count=degraded,
            scope_complete=not unparsed and not degraded)
        project_meta = await self.detect_project_meta(repo_path, file_index)
        return {
            "file_index": file_index,
            "symbol_index": symbol_index,
            "repository_graph": repository_graph,
            "project_meta": project_meta,
        }

    async def build_file_index(
        self, repo_path: str, *, changed_paths: frozenset[str] | None = None
    ) -> list[dict[str, Any]]:
        try:
            return await asyncio.to_thread(
                self._build_file_index_sync, repo_path, changed_paths)
        except asyncio.CancelledError:
            self._cancel_event.set()
            raise

    def _analyze(
        self, file_path: Path, relative_path: str, *, cache_key: str | None = None,
    ) -> FileAnalysis:
        if self._cancel_event.is_set():
            raise ParserCancelled()
        return default_language_registry.analyze(
            file_path, relative_path, cancel_event=self._cancel_event, cache_key=cache_key)

    def _analyze_cached(
        self, file_path: Path, relative_path: str, language: str, size: int,
        blob_sha: str | None,
    ) -> FileAnalysis:
        """Blob 缓存复用隔离入口的故障优先、内容校验与取消检查。"""
        key = None
        if blob_sha:
            key = hashlib.sha256(json.dumps({
                "cache": ANALYSIS_CACHE_PROTOCOL, "parser": parser_fingerprint(),
                "path": relative_path, "language": language, "blob": blob_sha, "size": size,
            }, sort_keys=True).encode()).hexdigest()
        return self._analyze(file_path, relative_path, cache_key=key)

    def _build_file_index_sync(
        self, repo_path: str, changed_paths: frozenset[str] | None = None,
    ) -> list[dict[str, Any]]:
        """遍历仓库构建文件索引；限定范围时只解析变更文件及其直接导入目标。"""
        root = Path(repo_path)
        tracked_blobs = list_git_tracked_blobs(root)
        tracked_files = set(tracked_blobs)
        cacheable = ({path: blob for path, blob in tracked_blobs.items() if blob}
                     if tracked_blobs and git_worktree_matches_index(root) else {})
        enforce_tracked = (root / ".git").exists()
        files: list[tuple[str, Path, str, int]] = []
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
                    size = file_path_obj.stat().st_size
                except OSError:
                    size = 0
                files.append((rel_path, file_path_obj, _detect_language(filename), size))

        analyses = self._analyze_files(files, changed_paths, cacheable)
        index: list[dict[str, Any]] = []
        for rel_path, _, language, size in files:
            analysis = analyses.get(rel_path)
            if analysis is None:
                index.append({
                    "path": rel_path,
                    "language": language,
                    "size": size,
                    "imports": [],
                    "import_refs": [],
                    "python_namespace": {},
                    "analysis_level": 0,
                    "parser_id": "text.v1" if language == "unknown" else "unparsed",
                    "diagnostics": [],
                })
                continue
            if self._symbol_index is not None:
                self._symbol_index.extend(symbol.to_index_entry() for symbol in analysis.symbols)
            index.append({
                "path": rel_path,
                "language": analysis.language_id,
                "size": size,
                "imports": sorted({item.module for item in analysis.imports}),
                "import_refs": [item.model_dump(mode="json") for item in analysis.imports],
                "python_namespace": analysis.python_namespace,
                "analysis_level": int(analysis.level),
                "parser_id": analysis.parser_id,
                "diagnostics": analysis.diagnostics,
            })
        if changed_paths is not None and analyses:
            scanned = {}
            entries = {item["path"]: item for item in index}
            for path, file_path, language, _ in files:
                if self._cancel_event.is_set():
                    raise ParserCancelled()
                if path not in analyses and language != "unknown":
                    scanned[path] = _scan_imports(
                        file_path, language, cancel_event=self._cancel_event,
                        diagnostics=entries[path]["diagnostics"])
            hints = reverse_import_hints(index, scanned, analyses)
            for item in index:
                if item["path"] in hints:
                    item["import_hints"] = hints[item["path"]]
        return sorted(index, key=lambda f: f["path"])

    def _analyze_files(
        self, files: list[tuple[str, Path, str, int]],
        changed_paths: frozenset[str] | None, cacheable: dict[str, str],
    ) -> dict[str, FileAnalysis]:
        by_path = {path: (file_path, language, size)
                   for path, file_path, language, size in files}
        if changed_paths is None:
            selected = {path for path, (_, language, _) in by_path.items()
                        if language != "unknown"}
        else:
            selected = {path for path in changed_paths
                        if path in by_path and by_path[path][1] != "unknown"}
        analyses = {
            path: self._analyze_cached(by_path[path][0], path, by_path[path][1],
                                       by_path[path][2], cacheable.get(path))
            for path in sorted(selected)
        }
        if changed_paths is not None and analyses:
            entries = [{
                "path": path, "language": language,
                "imports": (sorted({item.module for item in analyses[path].imports})
                            if path in analyses else []),
                "import_refs": ([item.model_dump(mode="json") for item in analyses[path].imports]
                                if path in analyses else []),
            } for path, (_, language, _) in by_path.items()]
            targets = resolved_import_targets(entries, analyses) | related_test_paths(entries, analyses)
            related = {
                path for path in targets
                if path in by_path and path not in analyses
                and by_path[path][1] != "unknown"
            }
            for path in sorted(related):
                analyses[path] = self._analyze_cached(
                    by_path[path][0], path, by_path[path][1], by_path[path][2],
                    cacheable.get(path))
        return analyses

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
    return sorted({ref["module"] for ref in _scan_imports(file_path, language)})


def _scan_imports(
    file_path: Path, language: str = "python", *, cancel_event=None, diagnostics=None,
) -> list[dict[str, str]]:
    """轻量扫描导入语句，供未解析文件的反向候选发现使用。"""
    if cancel_event is not None and cancel_event.is_set():
        raise ParserCancelled()
    try:
        with file_path.open("rb") as stream:
            source = stream.read(MAX_SOURCE_BYTES + 1)
    except OSError:
        if diagnostics is not None:
            diagnostics.append("import scan could not read source")
        return []
    if cancel_event is not None and cancel_event.is_set():
        raise ParserCancelled()
    if len(source) > MAX_SOURCE_BYTES:
        if diagnostics is not None:
            diagnostics.append("import scan source exceeds 4 MiB; skipped")
        return []
    content = source.decode("utf-8", errors="ignore")
    imports: list[dict[str, str]] = []
    if language == "python":
        try:
            tokens = tokenize.generate_tokens(io.StringIO(content).readline)
            for token in tokens:
                if cancel_event is not None and cancel_event.is_set():
                    raise ParserCancelled()
                if token.type != tokenize.NAME or token.string not in {"from", "import"}:
                    continue
                statement = []
                for part in tokens:
                    if cancel_event is not None and cancel_event.is_set():
                        raise ParserCancelled()
                    if part.type in {tokenize.NEWLINE, tokenize.ENDMARKER} or part.string == ";":
                        break
                    if part.type in {tokenize.NAME, tokenize.OP}:
                        statement.append(part.string)
                imports.extend(_python_import_refs(token.string, statement))
        except (tokenize.TokenError, IndentationError):
            if diagnostics is not None:
                diagnostics.append("import scan tokenization incomplete")
    elif language in {"javascript", "typescript"}:
        patterns = (
            r"\b(?:import|export)\s+(?:[^;]*?\s+from\s+)?[\"']([^\"']+)[\"']",
            r"\brequire\s*\(\s*[\"']([^\"']+)[\"']\s*\)",
            r"\bimport\s*\(\s*[\"']([^\"']+)[\"']\s*\)",
        )
        for pattern in patterns:
            imports.extend({"module": module} for module in re.findall(pattern, content))
    elif language == "java":
        imports.extend({"module": module} for module in re.findall(
            r"^\s*import\s+(?:static\s+)?([\w.]+)(?:\.\*)?\s*;", content, re.MULTILINE))
    elif language == "go":
        imports.extend({"module": module} for module in re.findall(
            r"\bimport\s+(?:[\w.]+\s+)?\"([^\"]+)\"", content))
        for block in re.findall(r"\bimport\s*\((.*?)\)", content, re.DOTALL):
            imports.extend({"module": module} for module in re.findall(
                r"(?:^|\n)\s*(?:[\w.]+\s+)?\"([^\"]+)\"", block))
    elif language == "rust":
        imports.extend({"module": module} for module in re.findall(
            r"^\s*use\s+([^;{]+)", content, re.MULTILINE))
        imports.extend({"module": module} for module in re.findall(
            r"^\s*mod\s+([A-Za-z_][A-Za-z0-9_]*)\s*;", content, re.MULTILINE))
    if cancel_event is not None and cancel_event.is_set():
        raise ParserCancelled()
    return imports


def _python_import_refs(kind: str, tokens: list[str]) -> list[dict[str, str]]:
    module = ""
    if kind == "from":
        if "import" not in tokens:
            return []
        boundary = tokens.index("import")
        module, tokens = "".join(tokens[:boundary]), tokens[boundary + 1:]
        if not module:
            return []
    refs = []
    parts: list[str] = []
    for token in [*tokens, ","]:
        if token in {"(", ")"}:
            continue
        if token != ",":
            parts.append(token)
            continue
        if "as" in parts:
            parts = parts[:parts.index("as")]
        name = "".join(parts)
        if name:
            refs.append({"module": module, "imported_name": name}
                        if kind == "from" else {"module": name})
        parts = []
    return refs


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
