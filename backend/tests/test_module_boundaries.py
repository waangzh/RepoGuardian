"""目录迁移后的依赖方向约束：基础层不反向依赖服务、服务与验证不依赖 API、业务模块不依赖图节点。"""

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"


def _module_imports(path: Path) -> list[tuple[int, str]]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
    return [(line, name) for line, name in found if name.startswith("app.")]


def _modules(*prefixes: str) -> list[Path]:
    return [path for path in APP.rglob("*.py")
            if any(str(path.relative_to(APP.parent)).replace("\\", ".").startswith(prefix) for prefix in prefixes)]


@pytest.mark.parametrize("path", _modules("app.models", "app.core", "app.evidence"), ids=str)
def test_foundation_does_not_depend_on_services(path: Path) -> None:
    offenders = [(line, name) for line, name in _module_imports(path) if name.startswith("app.services")]
    assert offenders == []


@pytest.mark.parametrize("path", _modules("app.services", "app.validation", "app.graph", "app.review", "app.llm"), ids=str)
def test_services_validation_and_rules_do_not_depend_on_api(path: Path) -> None:
    offenders = [(line, name) for line, name in _module_imports(path) if name.startswith("app.api")]
    assert offenders == []


@pytest.mark.parametrize("path", _modules("app.services", "app.review", "app.evidence"), ids=str)
def test_business_modules_do_not_import_graph_nodes(path: Path) -> None:
    offenders = [(line, name) for line, name in _module_imports(path) if name.startswith("app.graph.nodes")]
    assert offenders == []
