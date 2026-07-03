"""仓库索引器 —— 扫描仓库构建三级索引。

产出：
    1. file_index   — [{path, language, size, imports}, ...]
    2. symbol_index — 函数/类/方法定义，tree-sitter 解析含签名、调用关系
    3. project_meta — 语言、框架、测试目录、入口点

注意：symbol_index 依赖 tree-sitter + tree-sitter-python，仅在解析 .py 文件时可用。
"""

import json
import os
import re
from pathlib import Path
from typing import Any

from app.tools.base import BaseTool

# 扫描时跳过的目录和文件
_IGNORED_DIRS = frozenset({
    ".git", "venv", "node_modules", "dist", "build",
    "__pycache__", ".pytest_cache", ".coverage", ".mypy_cache",
    ".ruff_cache", ".tox", ".eggs", ".repoguardian",
})

_IGNORED_FILES = frozenset({".DS_Store", "Thumbs.db"})

# 项目配置文件（用于 detect_project_meta）
_PY_CONFIG_FILES = {
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "Makefile", "tox.ini", "Pipfile",
}

# 框架检测：import 关键字 → 框架名
_FRAMEWORK_HINTS = {
    "fastapi": {"fastapi", "starlette"},
    "flask": {"flask"},
    "django": {"django"},
    "sqlalchemy": {"sqlalchemy"},
}


class RepoIndexer(BaseTool):
    """仓库结构扫描器，构建文件级和符号级索引。"""
    name = "repo_indexer"
    description = "Scan repository structure and build file-level and symbol-level index."

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """执行完整扫描，返回三层索引。"""
        repo_path = kwargs["repo_path"]
        file_index = await self.build_file_index(repo_path)
        symbol_index = await self.build_symbol_index(repo_path)
        project_meta = await self.detect_project_meta(repo_path, file_index)
        return {
            "file_index": file_index,
            "symbol_index": symbol_index,
            "project_meta": project_meta,
        }

    async def build_file_index(self, repo_path: str) -> list[dict[str, Any]]:
        """遍历仓库目录，构建文件级索引（路径、语言、大小、导入）。"""
        index: list[dict[str, Any]] = []
        root = Path(repo_path)
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = Path(dirpath).relative_to(root)
            # 跳过忽略目录
            if rel_dir.parts and rel_dir.parts[0] in _IGNORED_DIRS:
                dirnames[:] = []
                continue
            dirnames[:] = sorted(d for d in dirnames if d not in _IGNORED_DIRS)
            for filename in sorted(filenames):
                if filename in _IGNORED_FILES:
                    continue
                file_path_obj = Path(dirpath) / filename
                rel_path = file_path_obj.relative_to(root).as_posix()
                try:
                    stat = file_path_obj.stat()
                    size = stat.st_size
                except OSError:
                    size = 0
                language = _detect_language(filename)
                imports = _extract_imports(file_path_obj) if language == "python" else []
                index.append({
                    "path": rel_path,
                    "language": language,
                    "size": size,
                    "imports": imports,
                })
        return sorted(index, key=lambda f: f["path"])

    async def build_symbol_index(self, repo_path: str) -> list[dict[str, Any]]:
        """使用 tree-sitter 解析 Python 文件，提取函数/类/方法符号。"""
        index: list[dict[str, Any]] = []
        try:
            from tree_sitter import Language, Parser
        except ImportError:
            index
        root = Path(repo_path)
        for dirpath, _, filenames in os.walk(root):
            rel_dir = Path(dirpath).relative_to(root)
            if rel_dir.parts and rel_dir.parts[0] in _IGNORED_DIRS:
                continue
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                file_path_obj = Path(dirpath) / filename
                rel_path = file_path_obj.relative_to(root).as_posix()
                try:
                    symbols = _parse_python_symbols(str(file_path_obj), rel_path)
                except Exception:
                    symbols = []
                index.extend(symbols)
        return index

    async def detect_project_meta(
        self, repo_path: str, file_index: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """检测项目元信息：语言、框架、测试目录、入口点、配置文件。"""
        if file_index is None:
            file_index = await self.build_file_index(repo_path)
        language = "python" if any(
            f["language"] == "python" for f in file_index
        ) else "unknown"

        framework = _detect_framework(repo_path)
        test_dirs = _find_test_dirs(repo_path)
        config_files: list[str] = []
        for cfg in _PY_CONFIG_FILES:
            if (Path(repo_path) / cfg).exists():
                config_files.append(cfg)

        entry_points: list[str] = []
        for entry in ["app/main.py", "main.py", "src/main.py", "run.py"]:
            if (Path(repo_path) / entry).exists():
                entry_points.append(entry)

        return {
            "language": language,
            "framework": framework,
            "test_framework": "pytest" if test_dirs else None,
            "entry_points": entry_points,
            "test_dirs": test_dirs,
            "config_files": config_files,
            "total_files": len(file_index),
        }


def _detect_language(filename: str) -> str:
    """根据文件扩展名检测编程语言。"""
    ext = Path(filename).suffix.lower()
    return {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".jsx": "javascript",
        ".tsx": "typescript",
        ".java": "java",
        ".go": "go",
        ".rs": "rust",
    }.get(ext, "unknown")


def _extract_imports(file_path: Path) -> list[str]:
    """提取 Python 文件的顶层模块导入名。"""
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    pattern = re.compile(r"^(?:from\s+(\S+)\s+import|import\s+(\S+))", re.MULTILINE)
    imports: list[str] = []
    for match in pattern.finditer(content):
        mod = match.group(1) or match.group(2)
        if mod:
            imports.append(mod.split(".")[0])
    return sorted(set(imports))


def _detect_framework(repo_path: str) -> str | None:
    """通过扫描所有 .py 文件的 import 语句检测使用的 Web 框架。"""
    all_imports: set[str] = set()
    root = Path(repo_path)
    for dirpath, _, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        if rel.parts and rel.parts[0] in _IGNORED_DIRS:
            continue
        for fname in filenames:
            if fname.endswith(".py"):
                for imp in _extract_imports(Path(dirpath) / fname):
                    all_imports.add(imp.lower())

    for framework, hints in _FRAMEWORK_HINTS.items():
        if hints & all_imports:
            return framework
    return None


def _find_test_dirs(repo_path: str) -> list[str]:
    """检测仓库根目录下是否存在测试目录。"""
    root = Path(repo_path)
    test_dirs: list[str] = []
    for candidate in ["tests", "test", "testing"]:
        if (root / candidate).is_dir():
            test_dirs.append(candidate)
    return test_dirs


def _parse_python_symbols(file_path: str, rel_path: str) -> list[dict[str, Any]]:
    """使用 tree-sitter 解析单个 Python 文件的符号（函数/类/方法）。"""
    try:
        import tree_sitter_python as tspython
        from tree_sitter import Language, Parser
    except ImportError:
        return []

    try:
        with open(file_path, "rb") as f:
            source = f.read()
    except Exception:
        return []

    py_lang = Language(tspython.language())
    parser = Parser(py_lang)
    tree = parser.parse(source)
    root_node = tree.root_node

    symbols: list[dict[str, Any]] = []
    source_str = source.decode("utf-8")
    source_lines = source_str.split("\n")

    for node in _walk(root_node):
        if node.type == "function_definition":
            name_node = node.child_by_field_name("name")
            if name_node is None:
                continue
            name = _node_text(name_node, source).decode("utf-8").strip()
            params_node = node.child_by_field_name("parameters")
            params_text = ""
            if params_node:
                params_text = _node_text(params_node, source).decode("utf-8")
            signature = f"def {name}{params_text}"
            calls = _extract_calls(node, source)
            symbols.append({
                "file": rel_path,
                "symbol": name,
                "type": "function",
                "start_line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "signature": signature,
                "docstring": _extract_docstring(node, source_lines),
                "calls": calls,
            })
        elif node.type == "class_definition":
            name_node = node.child_by_field_name("name")
            if name_node is None:
                continue
            name = _node_text(name_node, source).decode("utf-8").strip()
            symbols.append({
                "file": rel_path,
                "symbol": name,
                "type": "class",
                "start_line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "signature": f"class {name}",
                "docstring": _extract_docstring(node, source_lines),
                "calls": [],
            })
            # 递归提取类内方法
            body = node.child_by_field_name("body")
            if body is not None:
                for child in body.children:
                    if child.type == "function_definition":
                        mn = child.child_by_field_name("name")
                        if mn is None:
                            continue
                        mname = _node_text(mn, source).decode("utf-8").strip()
                        mp = child.child_by_field_name("parameters")
                        mparams = ""
                        if mp:
                            mparams = _node_text(mp, source).decode("utf-8")
                        mcalls = _extract_calls(child, source)
                        symbols.append({
                            "file": rel_path,
                            "symbol": f"{name}.{mname}",
                            "type": "method",
                            "start_line": child.start_point[0] + 1,
                            "end_line": child.end_point[0] + 1,
                            "signature": f"def {mname}{mparams}",
                            "docstring": _extract_docstring(child, source_lines),
                            "calls": mcalls,
                        })

    return symbols


def _walk(node):
    """递归遍历 AST 节点。"""
    yield node
    for child in node.children:
        yield from _walk(child)


def _node_text(node, source: bytes) -> bytes:
    """提取节点对应的源代码字节。"""
    return source[node.start_byte:node.end_byte]


def _extract_calls(node, source: bytes) -> list[str]:
    """从函数/方法节点中提取所有函数调用名。"""
    calls: list[str] = []
    for child in node.children:
        if child.type == "call":
            func = child.child_by_field_name("function")
            if func:
                calls.append(
                    source[func.start_byte:func.end_byte].decode("utf-8").strip()
                )
        calls.extend(_extract_calls(child, source))
    return sorted(set(calls))


def _extract_docstring(node, source_lines: list[str]) -> str | None:
    """提取函数/类的 docstring（首个字符串表达式语句）。"""
    body = node.child_by_field_name("body")
    if body is None or not body.children:
        return None
    first = body.children[0]
    if first.type == "expression_statement" and first.children:
        expr = first.children[0]
        if expr.type == "string":
            text = source_lines[expr.start_point[0]][expr.start_point[1]:]
            return text.strip().strip('"').strip("'").strip()
    return None
