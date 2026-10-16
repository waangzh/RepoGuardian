"""Python 显式导入的保守词法证据；不加载或执行被审查模块。"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from app.languages.base import ImportRef


@dataclass
class _Scope:
    node: ast.AST
    parent: _Scope | None = None
    bindings: dict[str, list[object]] = field(default_factory=dict)
    opaque: set[str] = field(default_factory=set)
    dynamic: bool = False

    def bind(self, name, value):
        self.bindings.setdefault(name, []).append(value)


def python_binding_evidence(content: str, parser_id: str, *, include_namespace=False):
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError):
        return ([], {}, set(), {}) if include_namespace else ([], {}, set())
    root = _Scope(tree)
    scope = root
    imports, calls, definitions = [], [], []

    class Visitor(ast.NodeVisitor):
        def visit_Import(self, node):
            self.imports(node, None)

        def visit_ImportFrom(self, node):
            self.imports(node, "." * node.level + (node.module or ""))

        def imports(self, node, module):
            for alias in node.names:
                ref = ImportRef(module=alias.name if module is None else module,
                    imported_name=alias.name if module is not None else None,
                    local_name=alias.asname or (alias.name if module is not None else alias.name.split(".")[0]),
                    line=node.lineno, confidence=0.99, parser_id=parser_id,
                    scope_start_line=getattr(scope.node, "lineno", 1),
                    scope_end_line=getattr(scope.node, "end_lineno", len(content.splitlines())))
                imports.append(ref)
                direct = node in getattr(scope.node, "body", []) and alias.name != "*"
                scope.bind(ref.local_name, ref if direct else node)
                if alias.name == "*":
                    scope.dynamic = True

        def visit_Name(self, node):
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                scope.bind(node.id, node)

        def visit_Attribute(self, node):
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                value = node
                while isinstance(value, ast.Attribute):
                    value = value.value
                if isinstance(value, ast.Name):
                    current = scope
                    while current is not None:
                        current.opaque.add(value.id)
                        current = current.parent
            self.generic_visit(node)

        def visit_Global(self, node):
            current = scope
            while current is not None:
                current.opaque.update(node.names)
                current = current.parent

        visit_Nonlocal = visit_Global

        def visit_Call(self, node):
            if isinstance(node.func, ast.Name) and node.func.id in {
                "exec", "eval", "globals", "locals", "setattr", "delattr", "__import__",
            }:
                scope.dynamic = True
                root.dynamic = True
            calls.append((node, scope))
            self.generic_visit(node)

        def visit_FunctionDef(self, node):
            nonlocal scope
            scope.bind(node.name, node)
            definitions.append((node, scope))
            for value in [*node.decorator_list, *node.args.defaults,
                          *(value for value in node.args.kw_defaults if value is not None)]:
                self.visit(value)
            previous = scope
            scope = _Scope(node, previous)
            for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
                        *([node.args.vararg] if node.args.vararg else []),
                        *([node.args.kwarg] if node.args.kwarg else [])]:
                scope.bind(arg.arg, arg)
            for statement in node.body:
                self.visit(statement)
            scope = previous

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            nonlocal scope
            scope.bind(node.name, node)
            for value in [*node.decorator_list, *node.bases, *(kw.value for kw in node.keywords)]:
                self.visit(value)
            previous = scope
            scope = _Scope(node, previous)
            for statement in node.body:
                self.visit(statement)
            scope = previous

        def visit_Lambda(self, node):
            nonlocal scope
            previous = scope
            scope = _Scope(node, previous, dynamic=True)
            self.generic_visit(node)
            scope = previous

        visit_ListComp = visit_Lambda
        visit_SetComp = visit_Lambda
        visit_DictComp = visit_Lambda
        visit_GeneratorExp = visit_Lambda

        def visit_ExceptHandler(self, node):
            if node.name:
                scope.bind(node.name, node)
            self.generic_visit(node)

        def visit_MatchAs(self, node):
            if node.name:
                scope.bind(node.name, node)
            self.generic_visit(node)

        visit_MatchStar = visit_MatchAs

        def visit_MatchMapping(self, node):
            if node.rest:
                scope.bind(node.rest, node)
            self.generic_visit(node)

    Visitor().visit(tree)
    evidence = {}
    for call, owner in calls:
        expression = ast.unparse(call.func)
        parts = expression.split(".")
        current = owner
        binding, reason = None, "import_binding_missing"
        while current is not None:
            if current.dynamic or parts[0] in current.opaque:
                reason = "dynamic_or_nonlocal_binding"
                break
            values = current.bindings.get(parts[0], [])
            if values:
                if len(values) == 1 and isinstance(values[0], ImportRef) and values[0].line < call.lineno:
                    binding, reason = values[0], None
                else:
                    reason = "shadowed_or_conditional_binding"
                break
            current = current.parent
            while current is not None and isinstance(current.node, ast.ClassDef):
                current = current.parent
        evidence[(call.lineno, expression)] = {"import_binding": binding,
            "binding_reason": reason, "scope_start_line": getattr(owner.node, "lineno", 1)}
    stable = {node.lineno for node, owner in definitions if owner is root
              and node in tree.body and len(root.bindings[node.name]) == 1
              and not root.dynamic and node.name not in root.opaque and not node.decorator_list}
    result = (imports, evidence, stable)
    namespace = {"complete": True, "bound_names": sorted(root.bindings),
                 "opaque_names": sorted(root.opaque), "dynamic": root.dynamic}
    return (*result, namespace) if include_namespace else result
