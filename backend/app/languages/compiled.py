"""Static syntax indexing for Go, Java and Rust; no compiler or target code runs."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from app.languages.base import CallRef, ImportRef, SymbolRef

_JAVA_TYPES = {
    "class_declaration": "class", "interface_declaration": "interface",
    "enum_declaration": "enum", "record_declaration": "record",
    "annotation_type_declaration": "annotation",
}
_RUST_TYPES = {
    "struct_item": "struct", "enum_item": "enum", "trait_item": "trait",
    "type_item": "type", "associated_type": "type", "mod_item": "module",
    "const_item": "constant", "static_item": "constant",
}
_CALLABLES = {
    "function_declaration", "method_declaration", "function_item", "function_signature_item",
    "constructor_declaration", "compact_constructor_declaration", "method_elem",
    "annotation_type_element_declaration",
}
_SCOPE_BOUNDARIES = _CALLABLES | set(_JAVA_TYPES) | set(_RUST_TYPES) | {
    "func_literal", "closure_expression", "lambda_expression", "class_body",
    "impl_item", "macro_definition", "token_tree", "type_spec", "type_alias",
}


def _text(node: Any, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8") if node is not None else ""


def _walk(node: Any) -> Iterator[Any]:
    yield node
    for child in node.named_children:
        yield from _walk(child)


def _owned_nodes(node: Any) -> Iterator[Any]:
    yield node
    for child in node.named_children:
        if child.type not in _SCOPE_BOUNDARIES:
            yield from _owned_nodes(child)


def _identifier(node: Any, source: bytes) -> str:
    if node is None:
        return ""
    if node.type == "generic_function":
        return _identifier(node.child_by_field_name("function"), source)
    if node.type in {"generic_type", "pointer_type", "reference_type"}:
        inner = node.child_by_field_name("type")
        return _identifier(inner or node.named_children[-1], source)
    if node.type in {"scoped_type_identifier", "scoped_identifier"}:
        return "::".join(filter(None, (
            _identifier(node.child_by_field_name("path"), source),
            _identifier(node.child_by_field_name("name"), source),
        )))
    return _text(node, source).removeprefix("r#")


def _simple_callee(node: Any, source: bytes) -> str:
    for field in ("name", "field"):
        child = node.child_by_field_name(field)
        if child is not None:
            return _identifier(child, source)
    if node.type == "generic_function":
        return _simple_callee(node.child_by_field_name("function"), source)
    return _identifier(node, source).replace("::", ".").rsplit(".", 1)[-1]


def _calls(body: Any, source: bytes, parser_id: str, start: int) -> list[CallRef]:
    calls = []
    if body is None:
        return calls
    for node in _owned_nodes(body):
        ambiguous = False
        if node.type == "call_expression":
            function = node.child_by_field_name("function")
            if function is None:
                continue
            callee, simple = _text(function, source), _simple_callee(function, source)
        elif node.type == "type_conversion_expression":
            function = node.child_by_field_name("type")
            if function is None or function.type != "generic_type":
                continue
            # Go's grammar cannot distinguish F[T](x) from a generic conversion.
            callee, simple = _text(function, source), _simple_callee(function, source)
            ambiguous = True
        elif node.type == "method_invocation":
            name = node.child_by_field_name("name")
            receiver = node.child_by_field_name("object")
            simple = _text(name, source)
            callee = f"{_text(receiver, source)}.{simple}" if receiver else simple
        elif node.type == "object_creation_expression":
            function = node.child_by_field_name("type")
            callee, simple = _text(function, source), _simple_callee(function, source)
        else:
            continue
        if callee and simple:
            calls.append(CallRef(callee=callee, simple_name=simple,
                line=node.start_point.row + 1, confidence=0.65 if ambiguous else 0.92,
                parser_id=parser_id, scope_start_line=start,
                binding_reason="syntax_ambiguous_type_or_call" if ambiguous else "syntax_only"))
    return sorted(calls, key=lambda call: (call.line or 0, call.callee))


_DOCSTRING_COMMENT_TYPES = {"comment", "block_comment", "line_comment", "documentation_comment"}


def _docstring(node: Any, source: bytes) -> str | None:
    """紧邻声明上方且行号连续的注释块作为文档；行尾注释不归给下一个声明。"""
    previous = node.prev_sibling
    expected_line = node.start_point.row
    parts: list[str] = []
    while (previous is not None and previous.type in _DOCSTRING_COMMENT_TYPES
           and previous.end_point.row == expected_line - 1):
        parts.append(_text(previous, source))
        expected_line = previous.start_point.row
        previous = previous.prev_sibling
    if not parts:
        return None
    text = " ".join(reversed(parts))
    for prefix in ("/**", "/*", "///", "//!", "//", "#", "*"):
        text = text.replace(prefix, " ")
    text = " ".join(text.replace("*/", " ").split())
    return text[:600] or None


def _signature(node: Any, source: bytes, body: Any = None) -> str:
    end = body.start_byte if body is not None else node.end_byte
    return source[node.start_byte:end].decode("utf-8").strip().rstrip(";").rstrip()


def _symbol(node: Any, source: bytes, path: str, parser_id: str, name: str,
            kind: str, container: str | None = None, *, body: Any = None,
            exported: bool | None = None, signature: str | None = None) -> SymbolRef:
    qualified = f"{container}.{name}" if container else name
    start = node.start_point.row + 1
    return SymbolRef(file=path, name=qualified, kind=kind,
        symbol_id=f"{path}::{qualified}@{node.start_byte}", start_line=start,
        end_line=max(start, node.end_point.row + (1 if node.end_point.column else 0)),
        signature=signature if signature is not None else _signature(node, source, body),
        docstring=_docstring(node, source),
        calls=_calls(body, source, parser_id, start) if node.type in _CALLABLES else [],
        confidence=0.98, parser_id=parser_id, container=container, exported=exported)


def _go_symbols(root: Any, source: bytes, path: str, parser_id: str) -> list[SymbolRef]:
    symbols = []
    for node in _walk(root):
        kind = None
        name_node = node.child_by_field_name("name")
        container = None
        body = node.child_by_field_name("body")
        if node.type in {"function_declaration", "method_declaration", "method_elem"}:
            kind = "function" if node.type == "function_declaration" else "method"
            receiver = node.child_by_field_name("receiver")
            if receiver is not None:
                parameter = next(iter(receiver.named_children), None)
                container = _identifier(parameter.child_by_field_name("type"), source)
            elif node.type == "method_elem":
                parent = node.parent
                while parent is not None and parent.type not in {"type_spec", "type_alias"}:
                    parent = parent.parent
                if parent is not None:
                    container = _text(parent.child_by_field_name("name"), source)
        elif node.type in {"type_spec", "type_alias"}:
            type_node = node.child_by_field_name("type")
            kind = "type" if node.type == "type_alias" else {
                "struct_type": "struct", "interface_type": "interface",
            }.get(type_node.type, "type")
            if type_node.type in {"struct_type", "interface_type"}:
                body = next((child for child in type_node.children if child.type == "{"), None)
                if body is None:
                    body = next(iter(type_node.named_children), None)
        elif node.type == "const_spec":
            parent = node.parent
            while parent is not None and parent.type not in {"source_file", "block"}:
                parent = parent.parent
            if parent is None or parent.type != "source_file":
                continue
            for name in node.children_by_field_name("name"):
                if not name.is_named:
                    continue
                text = _text(name, source)
                symbols.append(_symbol(node, source, path, parser_id, text, "constant",
                    exported=text[0].isupper(), signature="const " + _text(node, source)))
        if kind and name_node is not None:
            name = _text(name_node, source)
            symbols.append(_symbol(node, source, path, parser_id, name, kind, container,
                body=body, exported=name[0].isupper()))
    return symbols


def _java_symbols(root: Any, source: bytes, path: str, parser_id: str) -> list[SymbolRef]:
    symbols = []

    def visit(node: Any, container: str | None = None, interface: bool = False) -> None:
        if node.type == "object_creation_expression":
            return  # Anonymous type members have no stable named container.
        name_node = node.child_by_field_name("name")
        kind = _JAVA_TYPES.get(node.type)
        if kind is None and node.type in _CALLABLES:
            kind = "constructor" if "constructor" in node.type else "method"
        body = node.child_by_field_name("body")
        modifiers = next((child for child in node.named_children if child.type == "modifiers"), None)
        modifier_types = {child.type for child in modifiers.children} if modifiers else set()
        if kind and name_node is not None:
            name = _text(name_node, source)
            symbols.append(_symbol(node, source, path, parser_id, name, kind, container,
                body=body, exported="public" in modifier_types or interface))
            container = f"{container}.{name}" if container else name
            interface = node.type in {"interface_declaration", "annotation_type_declaration"}
        elif node.type in {"field_declaration", "constant_declaration"}:
            if "final" in modifier_types or node.type == "constant_declaration":
                for declarator in node.children_by_field_name("declarator"):
                    name = declarator.child_by_field_name("name")
                    symbols.append(_symbol(node, source, path, parser_id, _text(name, source),
                        "constant", container, exported="public" in modifier_types or interface,
                        signature=_text(node, source)))
        for child in node.named_children:
            visit(child, container, interface)

    visit(root)
    return symbols


def _rust_symbols(root: Any, source: bytes, path: str, parser_id: str) -> list[SymbolRef]:
    symbols = []

    def visit(node: Any, container: str | None = None, in_impl: bool = False) -> None:
        if node.type in {"macro_definition", "token_tree"}:
            return
        if node.type == "impl_item":
            target = _identifier(node.child_by_field_name("type"), source).replace("::", ".")
            container = f"{container}.{target}" if container else target
            in_impl = True
        kind = _RUST_TYPES.get(node.type)
        if node.type in {"function_item", "function_signature_item"}:
            kind = "method" if in_impl else "function"
        name_node = node.child_by_field_name("name")
        if kind and name_node is not None:
            name = _identifier(name_node, source)
            visibility = next((child for child in node.named_children if child.type == "visibility_modifier"), None)
            body = node.child_by_field_name("body")
            symbols.append(_symbol(node, source, path, parser_id, name, kind, container,
                body=body, exported=_text(visibility, source) == "pub"))
            container = f"{container}.{name}" if container else name
            in_impl = node.type == "trait_item"
        for child in node.named_children:
            visit(child, container, in_impl)

    visit(root)
    return symbols


def _imports(root: Any, source: bytes, language: str, parser_id: str) -> list[ImportRef]:
    refs = []

    def add(node: Any, module: str, kind: str = "import", local: str | None = None,
            imported: str | None = None) -> None:
        refs.append(ImportRef(module=module, kind=kind, line=node.start_point.row + 1,
            confidence=0.97, parser_id=parser_id, local_name=local, imported_name=imported))

    def rust_use(node: Any, prefix: str = "") -> None:
        if node.type == "scoped_use_list":
            part = _text(node.child_by_field_name("path"), source)
            rust_use(node.child_by_field_name("list"), "::".join(filter(None, (prefix, part))))
        elif node.type == "use_list":
            for child in node.named_children:
                rust_use(child, prefix)
        elif node.type == "use_as_clause":
            part = _text(node.child_by_field_name("path"), source)
            module = prefix if part == "self" else "::".join(filter(None, (prefix, part)))
            add(node, module, local=_identifier(node.child_by_field_name("alias"), source),
                imported=module.rsplit("::", 1)[-1])
        else:
            part = _text(node, source)
            module = prefix if part == "self" else "::".join(filter(None, (prefix, part)))
            add(node, module, "wildcard" if node.type == "use_wildcard" else "import",
                imported=module.rsplit("::", 1)[-1])

    for node in _walk(root):
        if language == "go" and node.type == "import_spec":
            literal = node.child_by_field_name("path")
            module = _text(literal, source)[1:-1]
            if literal.type == "interpreted_string_literal":
                import ast

                module = ast.literal_eval(_text(literal, source))
            local = _text(node.child_by_field_name("name"), source) or None
            add(node, module, "wildcard" if local == "." else "import", local)
        elif language == "java" and node.type == "import_declaration":
            target = next((child for child in node.named_children
                           if child.type in {"identifier", "scoped_identifier"}), None)
            wildcard = any(child.type == "asterisk" for child in node.named_children)
            static = any(child.type == "static" for child in node.children)
            module = _text(target, source) + (".*" if wildcard else "")
            add(node, module, "static_import" if static else "import",
                imported="*" if wildcard else module.rsplit(".", 1)[-1])
        elif language == "rust" and node.type == "use_declaration":
            rust_use(node.child_by_field_name("argument"))
        elif language == "rust" and node.type == "mod_item" and node.child_by_field_name("body") is None:
            add(node, _text(node.child_by_field_name("name"), source), "module")
    return sorted(refs, key=lambda ref: (ref.module, ref.line or 0))


def analyze_compiled(root: Any, source: bytes, path: str, language: str,
                     parser_id: str) -> tuple[list[SymbolRef], list[ImportRef]]:
    extractor = {"go": _go_symbols, "java": _java_symbols, "rust": _rust_symbols}[language]
    symbols = extractor(root, source, path, parser_id)
    return sorted(symbols, key=lambda symbol: int((symbol.symbol_id or "@0").rsplit("@", 1)[-1])), _imports(
        root, source, language, parser_id)
