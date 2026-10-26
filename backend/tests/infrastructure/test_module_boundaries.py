"""目录迁移后的依赖方向约束：基础层不反向依赖服务、服务与验证不依赖 API、业务模块不依赖图节点。"""

import ast
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace

import pytest

APP = Path(__file__).resolve().parents[2] / "app"


def _module_imports(path: Path) -> list[tuple[int, str]]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
    return [(line, name) for line, name in found if name.startswith("app.")]


def _modules(*prefixes: str) -> list[Path]:
    matched = []
    for path in APP.rglob("*.py"):
        name = ".".join(path.relative_to(APP.parent).with_suffix("").parts)
        if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes):
            matched.append(path)
    assert matched, f"No modules matched boundary prefixes: {prefixes}"
    return matched


# 已知接缝：discussion 选择需要流程层的容量裁剪，只在该契约分支内延迟导入。
SERVICES_IMPORT_EXCEPTIONS = {
    ("app/review/runtime_spec.py", "app.services.discussions.pr_discussions"),
}


@pytest.mark.parametrize("path", _modules("app.models", "app.core", "app.evidence", "app.review"), ids=str)
def test_foundation_does_not_depend_on_services(path: Path) -> None:
    relative = path.relative_to(APP.parent).as_posix()
    offenders = [(line, name) for line, name in _module_imports(path)
                 if name.startswith("app.services") and (relative, name) not in SERVICES_IMPORT_EXCEPTIONS]
    assert offenders == []


@pytest.mark.parametrize("path", _modules("app.services", "app.validation", "app.graph", "app.review", "app.llm"), ids=str)
def test_services_validation_and_rules_do_not_depend_on_api(path: Path) -> None:
    offenders = [(line, name) for line, name in _module_imports(path) if name.startswith("app.api")]
    assert offenders == []


@pytest.mark.parametrize("path", _modules("app.services", "app.review", "app.evidence"), ids=str)
def test_business_modules_do_not_import_graph_nodes(path: Path) -> None:
    offenders = [(line, name) for line, name in _module_imports(path) if name.startswith("app.graph.nodes")]
    assert offenders == []


@pytest.mark.parametrize("path_type", [PurePosixPath, PureWindowsPath])
def test_boundary_selection_is_platform_independent(monkeypatch, path_type):
    root = path_type("/workspace/backend/app")
    paths = [
        root / "models/review/common.py",
        root / "models_extra/example.py",
        root / "services/review/review_service.py",
    ]
    monkeypatch.setitem(
        globals(), "APP", SimpleNamespace(parent=root.parent, rglob=lambda pattern: paths),
    )
    assert _modules("app.models") == [paths[0]]
    assert _modules("app.services") == [paths[2]]


def test_empty_boundary_selection_fails_instead_of_skipping(monkeypatch):
    root = PurePosixPath("/workspace/backend/app")
    monkeypatch.setitem(
        globals(), "APP", SimpleNamespace(parent=root.parent, rglob=lambda pattern: []),
    )
    with pytest.raises(AssertionError, match="No modules matched"):
        _modules("app.models")
