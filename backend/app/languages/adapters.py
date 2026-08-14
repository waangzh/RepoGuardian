"""内置语言分析适配器：L2 Tree-sitter，失败时确定性降级到 L1。"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from app.languages.base import (
    AnalysisLevel,
    CallRef,
    FileAnalysis,
    ImportRef,
    SymbolRef,
    default_test_path,
)

_CALL_PATTERN = re.compile(r"\b([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*\(")
_IGNORED_CALLS = {"if", "for", "while", "switch", "catch", "return", "func", "fn", "function"}

_DECLARATION_PATTERNS: dict[str, tuple[tuple[str, str], ...]] = {
    "python": (
        ("function", r"^\s*(?:async\s+)?def\s+([A-Za-z_][\w]*)\s*\("),
        ("class", r"^\s*class\s+([A-Za-z_][\w]*)\b"),
        ("constant", r"^\s*([A-Z][A-Z0-9_]*)\s*="),
    ),
    "javascript": (
        ("function", r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"),
        ("class", r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)"),
        ("function", r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"),
        ("constant", r"^\s*(?:export\s+)?const\s+([A-Z][A-Z0-9_]*)\b"),
    ),
    "typescript": (
        ("function", r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"),
        ("class", r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)"),
        ("interface", r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)"),
        ("type", r"^\s*(?:export\s+)?type\s+([A-Za-z_$][\w$]*)"),
        ("function", r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"),
        ("constant", r"^\s*(?:export\s+)?const\s+([A-Z][A-Z0-9_]*)\b"),
    ),
    "java": (
        ("class", r"^\s*(?:public\s+|protected\s+|private\s+|abstract\s+|final\s+)*(?:class|record|enum)\s+([A-Za-z_$][\w$]*)"),
        ("interface", r"^\s*(?:public\s+)?interface\s+([A-Za-z_$][\w$]*)"),
        ("method", r"^\s*(?:public|protected|private)\s+(?:static\s+)?(?:final\s+)?[\w<>,.?\[\]]+\s+([A-Za-z_$][\w$]*)\s*\("),
    ),
    "go": (
        ("function", r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_][\w]*)\s*\("),
        ("type", r"^\s*type\s+([A-Za-z_][\w]*)\s+(?:struct|interface)\b"),
    ),
    "rust": (
        ("function", r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_][\w]*)"),
        ("class", r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum)\s+([A-Za-z_][\w]*)"),
        ("interface", r"^\s*(?:pub(?:\([^)]*\))?\s+)?trait\s+([A-Za-z_][\w]*)"),
    ),
}


class RegexLanguageAdapter:
    max_level = AnalysisLevel.heuristic

    def __init__(self, language_id: str, extensions: set[str]) -> None:
        self.language_id = language_id
        self.extensions = frozenset(extensions)
        self.rule_pack_id = f"review.language.{language_id}"

    def analyze(self, file_path: Path, relative_path: str) -> FileAnalysis:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
        parser_id = f"regex.{self.language_id}.v1"
        imports = _regex_imports(content, self.language_id, parser_id)
        lines = content.splitlines()
        symbols: list[SymbolRef] = []
        seen: set[tuple[str, int]] = set()
        for line_no, line in enumerate(lines, start=1):
            for kind, pattern in _DECLARATION_PATTERNS.get(self.language_id, ()):
                match = re.match(pattern, line)
                if not match or (match.group(1), line_no) in seen:
                    continue
                seen.add((match.group(1), line_no))
                end_line = _declaration_end_line(lines, line_no)
                body = "\n".join(lines[line_no - 1:end_line])
                calls = _text_calls(body, parser_id, confidence=0.55)
                symbols.append(SymbolRef(
                    file=relative_path,
                    name=match.group(1),
                    kind=kind,
                    start_line=line_no,
                    end_line=end_line,
                    signature=line.strip()[:500],
                    calls=calls,
                    confidence=0.58,
                    parser_id=parser_id,
                    exported=line.lstrip().startswith(("export ", "pub ")),
                ))
                break
        return FileAnalysis(
            language_id=self.language_id,
            level=AnalysisLevel.heuristic,
            parser_id=parser_id,
            imports=imports,
            symbols=symbols,
        )

    def is_test_path(self, path: str) -> bool:
        return default_test_path(path)


class TreeSitterLanguageAdapter(RegexLanguageAdapter):
    max_level = AnalysisLevel.semantic

    def __init__(
        self,
        language_id: str,
        extensions: set[str],
        grammar_loader: Callable[[str], Any],
    ) -> None:
        super().__init__(language_id, extensions)
        self._grammar_loader = grammar_loader

    def analyze(self, file_path: Path, relative_path: str) -> FileAnalysis:
        try:
            source = file_path.read_bytes()
            from tree_sitter import Language, Parser

            language = Language(self._grammar_loader(file_path.suffix.casefold()))
            parser = Parser(language)
            tree = parser.parse(source)
            if tree.root_node.has_error:
                raise ValueError("tree-sitter produced an error node")
            parser_id = f"tree-sitter.{self.language_id}.v1"
            if self.language_id == "python":
                symbols = _python_symbols(tree.root_node, source, relative_path, parser_id)
                imports = _regex_imports(source.decode("utf-8", errors="ignore"), "python", parser_id, 0.99)
            else:
                symbols = _ecmascript_symbols(tree.root_node, source, relative_path, parser_id)
                imports = _ecmascript_imports(tree.root_node, source, parser_id)
            return FileAnalysis(
                language_id=self.language_id,
                level=AnalysisLevel.semantic,
                parser_id=parser_id,
                imports=imports,
                symbols=symbols,
            )
        except Exception as exc:
            fallback = super().analyze(file_path, relative_path)
            fallback.diagnostics.append(f"semantic parser unavailable; downgraded to L1: {type(exc).__name__}")
            return fallback


def load_python_grammar(_: str) -> Any:
    import tree_sitter_python

    return tree_sitter_python.language()


def load_typescript_grammar(extension: str) -> Any:
    import tree_sitter_typescript

    if extension == ".tsx":
        return tree_sitter_typescript.language_tsx()
    return tree_sitter_typescript.language_typescript()


def load_javascript_grammar(_: str) -> Any:
    import tree_sitter_javascript

    return tree_sitter_javascript.language()


def _walk(node: Any) -> Iterator[Any]:
    yield node
    for child in node.children:
        yield from _walk(child)


def _text(node: Any, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="ignore")


def _call_refs(node: Any, source: bytes, parser_id: str, node_type: str) -> list[CallRef]:
    calls: dict[tuple[str, int], CallRef] = {}
    for child in _walk(node):
        if child.type != node_type:
            continue
        function = child.child_by_field_name("function")
        if function is None:
            continue
        callee = _text(function, source).strip()
        if not callee:
            continue
        line = child.start_point[0] + 1
        calls[(callee, line)] = CallRef(
            callee=callee,
            simple_name=callee.rsplit(".", 1)[-1],
            line=line,
            confidence=0.92,
            parser_id=parser_id,
        )
    return sorted(calls.values(), key=lambda item: (item.line or 0, item.callee))


def _python_symbols(root: Any, source: bytes, relative_path: str, parser_id: str) -> list[SymbolRef]:
    symbols: list[SymbolRef] = []
    for node in _walk(root):
        if node.type not in {"function_definition", "class_definition"}:
            continue
        parent = node.parent
        if parent is not None and parent.type == "block" and parent.parent is not None:
            if parent.parent.type == "class_definition" and node.type == "function_definition":
                continue
        name_node = node.child_by_field_name("name")
        if name_node is None:
            continue
        name = _text(name_node, source).strip()
        params = node.child_by_field_name("parameters")
        kind = "function" if node.type == "function_definition" else "class"
        signature = f"def {name}{_text(params, source) if params else ''}" if kind == "function" else f"class {name}"
        calls = _call_refs(node, source, parser_id, "call") if kind == "function" else []
        symbols.append(SymbolRef(
            file=relative_path,
            name=name,
            kind=kind,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=signature,
            calls=calls,
            confidence=0.98,
            parser_id=parser_id,
        ))
        if kind == "class":
            body = node.child_by_field_name("body")
            for child in body.children if body is not None else ():
                if child.type != "function_definition":
                    continue
                method_name_node = child.child_by_field_name("name")
                if method_name_node is None:
                    continue
                method_name = _text(method_name_node, source).strip()
                method_params = child.child_by_field_name("parameters")
                symbols.append(SymbolRef(
                    file=relative_path,
                    name=f"{name}.{method_name}",
                    kind="method",
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                    signature=f"def {method_name}{_text(method_params, source) if method_params else ''}",
                    calls=_call_refs(child, source, parser_id, "call"),
                    confidence=0.98,
                    parser_id=parser_id,
                    container=name,
                ))
    return symbols


def _ecmascript_symbols(root: Any, source: bytes, relative_path: str, parser_id: str) -> list[SymbolRef]:
    symbols: list[SymbolRef] = []
    declaration_types = {
        "function_declaration": "function",
        "class_declaration": "class",
        "interface_declaration": "interface",
        "type_alias_declaration": "type",
        "enum_declaration": "enum",
    }
    for node in _walk(root):
        kind = declaration_types.get(node.type)
        name_node = node.child_by_field_name("name") if kind else None
        value_node = None
        if node.type == "variable_declarator":
            name_node = node.child_by_field_name("name")
            value_node = node.child_by_field_name("value")
            if value_node is not None and value_node.type in {"arrow_function", "function_expression"}:
                kind = "function"
            elif name_node is not None and _text(name_node, source).isupper():
                kind = "constant"
        if node.type == "method_definition":
            kind = "method"
            name_node = node.child_by_field_name("name")
        if kind is None or name_node is None:
            continue
        name = _text(name_node, source).strip()
        container = _nearest_class_name(node, source)
        indexed_name = f"{container}.{name}" if kind == "method" and container else name
        call_root = value_node or node
        signature = _text(node, source).splitlines()[0].strip()[:500]
        parent_text = _text(node.parent, source).lstrip()[:20] if node.parent is not None else ""
        symbols.append(SymbolRef(
            file=relative_path,
            name=indexed_name,
            kind=kind,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=signature,
            calls=_call_refs(call_root, source, parser_id, "call_expression"),
            confidence=0.96,
            parser_id=parser_id,
            exported=parent_text.startswith("export") or _text(node, source).lstrip().startswith("export"),
            container=container,
        ))
    return symbols


def _nearest_class_name(node: Any, source: bytes) -> str | None:
    parent = node.parent
    while parent is not None:
        if parent.type == "class_declaration":
            name = parent.child_by_field_name("name")
            return _text(name, source).strip() if name is not None else None
        parent = parent.parent
    return None


def _ecmascript_imports(root: Any, source: bytes, parser_id: str) -> list[ImportRef]:
    refs: dict[tuple[str, str, int], ImportRef] = {}
    for node in _walk(root):
        if node.type in {"import_statement", "export_statement"}:
            source_node = node.child_by_field_name("source")
            if source_node is None:
                continue
            module = _text(source_node, source).strip("'\"")
            kind = "export_from" if node.type == "export_statement" else "import"
        elif node.type == "call_expression":
            function = node.child_by_field_name("function")
            arguments = node.child_by_field_name("arguments")
            function_text = _text(function, source).strip() if function is not None else ""
            if function_text not in {"require", "import"} or arguments is None:
                continue
            match = re.search(r"[\"']([^\"']+)[\"']", _text(arguments, source))
            if not match:
                continue
            module = match.group(1)
            kind = "require" if function_text == "require" else "dynamic_import"
        else:
            continue
        line = node.start_point[0] + 1
        refs[(module, kind, line)] = ImportRef(
            module=module,
            kind=kind,
            line=line,
            confidence=0.97,
            parser_id=parser_id,
        )
    return sorted(refs.values(), key=lambda item: (item.module, item.line or 0))


def _regex_imports(
    content: str,
    language: str,
    parser_id: str,
    confidence: float = 0.7,
) -> list[ImportRef]:
    modules: list[tuple[str, str, int | None]] = []
    if language == "python":
        try:
            tree = ast.parse(content)
        except SyntaxError:
            tree = None
        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules.extend((alias.name, "import", node.lineno) for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    module = "." * node.level + (node.module or "")
                    if module:
                        modules.append((module, "import", node.lineno))
    elif language in {"javascript", "typescript"}:
        patterns = (
            ("import", r"\b(?:import|export)\s+(?:[^;]*?\s+from\s+)?[\"']([^\"']+)[\"']"),
            ("require", r"\brequire\s*\(\s*[\"']([^\"']+)[\"']\s*\)"),
            ("dynamic_import", r"\bimport\s*\(\s*[\"']([^\"']+)[\"']\s*\)"),
        )
        for kind, pattern in patterns:
            modules.extend((match.group(1), kind, content.count("\n", 0, match.start()) + 1) for match in re.finditer(pattern, content))
    elif language == "java":
        modules.extend((match.group(1), "import", content.count("\n", 0, match.start()) + 1) for match in re.finditer(r"^\s*import\s+(?:static\s+)?([\w.]+)(?:\.\*)?\s*;", content, re.MULTILINE))
    elif language == "go":
        modules.extend((match.group(1), "import", content.count("\n", 0, match.start()) + 1) for match in re.finditer(r"(?:^|\n)\s*(?:import\s+)?(?:[\w.]+\s+)?\"([^\"]+)\"", content))
    elif language == "rust":
        modules.extend((match.group(1), "import", content.count("\n", 0, match.start()) + 1) for match in re.finditer(r"^\s*use\s+([^;{]+)", content, re.MULTILINE))
        modules.extend((match.group(1), "module", content.count("\n", 0, match.start()) + 1) for match in re.finditer(r"^\s*mod\s+([A-Za-z_][A-Za-z0-9_]*)\s*;", content, re.MULTILINE))
    refs = {
        (module, kind, line): ImportRef(
            module=module,
            kind=kind,
            line=line,
            confidence=confidence,
            parser_id=parser_id,
        )
        for module, kind, line in modules
        if module
    }
    return sorted(refs.values(), key=lambda item: (item.module, item.line or 0))


def _text_calls(content: str, parser_id: str, confidence: float) -> list[CallRef]:
    calls: dict[tuple[str, int], CallRef] = {}
    for match in _CALL_PATTERN.finditer(content):
        callee = match.group(1)
        if callee.casefold() in _IGNORED_CALLS:
            continue
        line = content.count("\n", 0, match.start()) + 1
        calls[(callee, line)] = CallRef(
            callee=callee,
            simple_name=callee.rsplit(".", 1)[-1],
            line=line,
            confidence=confidence,
            parser_id=parser_id,
        )
    return sorted(calls.values(), key=lambda item: (item.line or 0, item.callee))


def _declaration_end_line(lines: list[str], start_line: int) -> int:
    depth = 0
    opened = False
    for index in range(start_line - 1, min(len(lines), start_line + 500)):
        line = re.sub(r"(?:\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*')", "", lines[index])
        depth += line.count("{") - line.count("}")
        opened = opened or "{" in line
        if opened and depth <= 0:
            return index + 1
        if not opened and line.rstrip().endswith((";", "=")):
            return index + 1
    return min(len(lines), start_line + 200)
