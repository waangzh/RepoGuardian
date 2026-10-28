import hashlib
import json
import sqlite3
import subprocess
import threading

import pytest

from app.languages import isolation, parser_cache
from app.languages.registry import default_language_registry
from app.tools.repo_indexer import RepoIndexer


def _write(path, text):
    path.write_text(text, encoding="utf-8", newline="\n")


def _git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "core.autocrlf", "false"],
                   check=True, capture_output=True)
    return repo


def _git_commit(repo, *paths):
    subprocess.run(["git", "-C", str(repo), "add", *paths], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c",
         "user.email=test@example.test", "commit", "-m", "fixture"],
        check=True, capture_output=True,
    )


@pytest.mark.asyncio
async def test_build_file_index_on_test_dir(tmp_path):
    (tmp_path / "main.py").write_text("print('hello')")
    (tmp_path / "README.md").write_text("# Test")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text("def test_hello(): pass")

    indexer = RepoIndexer()
    index = await indexer.build_file_index(str(tmp_path))

    paths = {f["path"] for f in index}
    assert "main.py" in paths
    assert "README.md" in paths
    assert "tests/test_main.py" in paths


async def test_large_index_analyzes_each_code_file_once_without_caching_documents(
    tmp_path, monkeypatch,
):
    from collections import Counter
    from app.languages.base import AnalysisLevel, FileAnalysis, SymbolRef
    from app.languages.registry import default_language_registry

    for i in range(520):
        (tmp_path / f"a{i:04}.md").write_text("# document", encoding="utf-8")
    for i in range(600):
        (tmp_path / f"z{i:04}.py").write_text("def entry(): pass\n", encoding="utf-8")
    calls = Counter()

    def analyze(path, relative_path, **kwargs):
        calls[relative_path] += 1
        assert path.suffix == ".py"
        return FileAnalysis(
            language_id="python", level=AnalysisLevel.semantic, parser_id="tree-sitter.python.v1",
            symbols=[SymbolRef(file=relative_path, name="entry", kind="function",
                               start_line=1, end_line=1, confidence=0.98,
                               parser_id="tree-sitter.python.v1")],
        )

    monkeypatch.setattr(default_language_registry, "analyze", analyze)
    result = await RepoIndexer().execute(repo_path=str(tmp_path))
    assert len(calls) == 600 and set(calls.values()) == {1}
    assert len(result["file_index"]) == 1120
    assert len(result["symbol_index"]) == 600
    documents = [item for item in result["file_index"] if item["language"] == "unknown"]
    assert len(documents) == 520
    assert all(item["analysis_level"] == 0 and item["imports"] == [] for item in documents)


async def test_combined_index_matches_standalone_file_and_symbol_scans(tmp_path):
    (tmp_path / "main.py").write_text("import math\ndef entry(x): return math.sqrt(x)\n")
    (tmp_path / "readme.md").write_text("# Readme")
    combined = await RepoIndexer().execute(repo_path=str(tmp_path))
    standalone = RepoIndexer()
    assert combined["file_index"] == await standalone.build_file_index(str(tmp_path))
    assert combined["symbol_index"] == await standalone.build_symbol_index(str(tmp_path))


@pytest.mark.asyncio
async def test_detect_project_meta(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'")
    (tmp_path / "tests").mkdir()

    indexer = RepoIndexer()
    file_index = await indexer.build_file_index(str(tmp_path))
    meta = await indexer.detect_project_meta(str(tmp_path), file_index)

    assert meta["language"] == "python"
    assert meta["framework"] == "fastapi"
    assert meta["test_framework"] == "pytest"
    assert "pyproject.toml" in meta["config_files"]


@pytest.mark.asyncio
async def test_build_symbol_index(tmp_path):
    (tmp_path / "app.py").write_text(
        'def hello(name: str) -> str:\n'
        '    """Say hello."""\n'
        '    return f"hi {name}"\n'
        '\n'
        'class Greeter:\n'
        '    def greet(self, name: str) -> str:\n'
        '        return hello(name)\n'
    )

    indexer = RepoIndexer()
    symbols = await indexer.build_symbol_index(str(tmp_path))

    names = {s["symbol"] for s in symbols}
    assert "hello" in names
    assert "Greeter" in names
    assert "Greeter.greet" in names
    # 定义预注入依赖签名与 docstring；缺失时只能退化为纯名称提示。
    indexed = {s["symbol"]: s for s in symbols}
    assert indexed["hello"]["signature"] == "def hello(name: str)"
    assert indexed["hello"]["docstring"] == "Say hello."
    assert indexed["Greeter.greet"]["signature"] == "def greet(self, name: str)"


@pytest.mark.asyncio
async def test_file_index_preserves_full_python_import_and_builds_import_edge(tmp_path):
    (tmp_path / "app" / "services").mkdir(parents=True)
    (tmp_path / "app" / "api").mkdir(parents=True)
    (tmp_path / "app" / "services" / "user.py").write_text(
        "class UserService:\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "app" / "api" / "users.py").write_text(
        "from app.services.user import UserService\n", encoding="utf-8"
    )

    index = await RepoIndexer().execute(repo_path=str(tmp_path))
    api = next(item for item in index["file_index"] if item["path"] == "app/api/users.py")
    assert api["imports"] == ["app.services.user"]
    assert any(
        edge["type"] == "imports"
        and edge["source"] == "app/api/users.py"
        and edge["target"] == "app/services/user.py"
        and edge["confidence"] >= 0.99
        for edge in index["repository_graph"]["edges"]
    )


@pytest.mark.asyncio
async def test_file_index_extracts_typescript_imports(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "client.ts").write_text(
        "import { User } from './models'\n", encoding="utf-8"
    )
    (tmp_path / "src" / "models.ts").write_text(
        "export interface User { id: string }\n", encoding="utf-8"
    )

    index = await RepoIndexer().execute(repo_path=str(tmp_path))
    client = next(item for item in index["file_index"] if item["path"] == "src/client.ts")
    assert client["imports"] == ["./models"]
    assert any(
        edge["type"] == "imports" and edge["target"] == "src/models.ts"
        for edge in index["repository_graph"]["edges"]
    )


@pytest.mark.asyncio
async def test_symbol_index_extracts_typescript_declarations(tmp_path):
    (tmp_path / "client.ts").write_text(
        "export interface User { id: string }\n"
        "export function loadUser(id: string) { return fetchUser(id) }\n"
        "export const DEFAULT_LIMIT = 20\n",
        encoding="utf-8",
    )

    symbols = await RepoIndexer().build_symbol_index(str(tmp_path))
    indexed = {(item["symbol"], item["type"]) for item in symbols}
    assert ("User", "interface") in indexed
    assert ("loadUser", "function") in indexed
    assert ("DEFAULT_LIMIT", "constant") in indexed
    load_user = next(item for item in symbols if item["symbol"] == "loadUser")
    assert "fetchUser" in load_user["calls"]
    assert load_user["docstring"] is None  # 无文档声明时不得编造说明文本。
    assert load_user["parser_id"].startswith("tree-sitter.typescript")
    assert load_user["confidence"] >= 0.9
    assert load_user["call_refs"][0]["simple_name"] == "fetchUser"


@pytest.mark.asyncio
async def test_typescript_tree_sitter_extracts_methods_arrows_and_import_refs(tmp_path):
    (tmp_path / "service.ts").write_text(
        "import { fetchUser } from './api'\n"
        "export const load = async (id: string) => fetchUser(id)\n"
        "export class UserService {\n"
        "  get(id: string) { return load(id) }\n"
        "}\n",
        encoding="utf-8",
    )

    result = await RepoIndexer().execute(repo_path=str(tmp_path))
    file_item = next(item for item in result["file_index"] if item["path"] == "service.ts")
    symbols = {item["symbol"]: item for item in result["symbol_index"]}

    assert file_item["analysis_level"] == 2
    assert file_item["parser_id"] == "tree-sitter.typescript.v1"
    assert file_item["import_refs"][0]["module"] == "./api"
    assert symbols["load"]["calls"] == ["fetchUser"]
    assert symbols["UserService.get"]["container"] == "UserService"
    assert symbols["UserService.get"]["calls"] == ["load"]


@pytest.mark.asyncio
async def test_javascript_and_tsx_use_their_tree_sitter_grammars(tmp_path):
    (tmp_path / "client.jsx").write_text(
        "export function render() { return createElement('main') }\n",
        encoding="utf-8",
    )
    (tmp_path / "view.tsx").write_text(
        "export const View = () => <main>Ready</main>\n",
        encoding="utf-8",
    )

    result = await RepoIndexer().execute(repo_path=str(tmp_path))
    files = {item["path"]: item for item in result["file_index"]}
    symbols = {item["symbol"]: item for item in result["symbol_index"]}

    assert files["client.jsx"]["parser_id"] == "tree-sitter.javascript.v1"
    assert files["view.tsx"]["parser_id"] == "tree-sitter.typescript.v1"
    assert symbols["render"]["calls"] == ["createElement"]
    assert symbols["View"]["parser_id"] == "tree-sitter.typescript.v1"


@pytest.mark.asyncio
async def test_go_symbol_index_carries_leading_comment_as_docstring(tmp_path):
    (tmp_path / "main.go").write_text(
        "package main\n"
        "\n"
        "// LoadUser reads a user by id.\n"
        "func LoadUser(id string) error {\n"
        "\treturn nil\n"
        "}\n",
        encoding="utf-8",
    )

    symbols = {item["symbol"]: item for item in await RepoIndexer().build_symbol_index(str(tmp_path))}
    assert symbols["LoadUser"]["docstring"] == "LoadUser reads a user by id."


@pytest.mark.asyncio
async def test_project_meta_exposes_language_analysis_capabilities(tmp_path):
    (tmp_path / "main.ts").write_text("export const main = () => true\n", encoding="utf-8")
    (tmp_path / "legacy.java").write_text("class Legacy {}\n", encoding="utf-8")

    indexer = RepoIndexer()
    file_index = await indexer.build_file_index(str(tmp_path))
    meta = await indexer.detect_project_meta(str(tmp_path), file_index)
    capabilities = {item["language_id"]: item for item in meta["analysis_capabilities"]}

    assert capabilities["typescript"]["max_level"] == 2
    assert capabilities["java"]["max_level"] == 2


@pytest.mark.asyncio
async def test_detect_project_meta_for_typescript_project(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.ts").write_text(
        "import { createApp } from 'vue'\n", encoding="utf-8"
    )
    (tmp_path / "src" / "main.test.ts").write_text(
        "import { test } from 'vitest'\n", encoding="utf-8"
    )
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")

    indexer = RepoIndexer()
    file_index = await indexer.build_file_index(str(tmp_path))
    meta = await indexer.detect_project_meta(str(tmp_path), file_index)

    assert meta["language"] == "typescript"
    assert meta["languages"] == ["typescript"]
    assert meta["framework"] == "vue"
    assert meta["test_framework"] == "vitest"
    assert meta["entry_points"] == ["src/main.ts"]
    assert meta["config_files"] == ["package.json", "tsconfig.json"]


@pytest.mark.asyncio
async def test_detect_project_meta_for_mixed_language_repository(tmp_path):
    (tmp_path / "frontend").mkdir()
    (tmp_path / "backend").mkdir()
    (tmp_path / "frontend" / "main.ts").write_text(
        "export const boot = () => true\n", encoding="utf-8"
    )
    (tmp_path / "frontend" / "view.tsx").write_text(
        "export const View = () => null\n", encoding="utf-8"
    )
    (tmp_path / "backend" / "main.py").write_text(
        "def main():\n    return True\n", encoding="utf-8"
    )

    indexer = RepoIndexer()
    file_index = await indexer.build_file_index(str(tmp_path))
    meta = await indexer.detect_project_meta(str(tmp_path), file_index)

    assert meta["language"] == "typescript"
    assert meta["languages"] == ["typescript", "python"]
    assert meta["language_counts"] == {"typescript": 2, "python": 1}
    assert meta["is_mixed_language"] is True


@pytest.mark.asyncio
async def test_execute_builds_repository_graph_off_event_loop_thread(monkeypatch):
    indexer = RepoIndexer()
    main_thread_id = threading.get_ident()
    seen: dict[str, int] = {}

    async def fake_build_file_index(repo_path: str):
        assert repo_path == "repo"
        return [{"path": "app.py", "language": "python", "imports": [],
                 "parser_id": "tree-sitter.python.v1", "analysis_level": 2}]

    async def fake_build_symbol_index(repo_path: str):
        assert repo_path == "repo"
        return []

    def fake_build_repository_graph(file_index, symbol_index):
        seen["thread_id"] = threading.get_ident()
        return {"files": file_index, "symbols": symbol_index, "edges": [], "metadata": {}}

    async def fake_detect_project_meta(repo_path: str, file_index):
        assert repo_path == "repo"
        assert file_index[0]["path"] == "app.py"
        return {"language": "python"}

    monkeypatch.setattr(indexer, "build_file_index", fake_build_file_index)
    monkeypatch.setattr(indexer, "build_symbol_index", fake_build_symbol_index)
    monkeypatch.setattr("app.tools.repo_indexer.build_repository_graph", fake_build_repository_graph)
    monkeypatch.setattr(indexer, "detect_project_meta", fake_detect_project_meta)

    result = await indexer.execute(repo_path="repo")

    assert result["repository_graph"]["edges"] == []
    assert seen["thread_id"] != main_thread_id


def test_repository_graph_links_test_and_config_files_without_full_cross_scan():
    from app.tools.repository_graph import build_repository_graph

    file_index = [
        {"path": "src/service/user.py", "language": "python", "imports": []},
        {"path": "src/service/account.py", "language": "python", "imports": []},
        {"path": "tests/test_user.py", "language": "python", "imports": []},
        {"path": "src/service/pyproject.toml", "language": "unknown", "imports": []},
        {"path": "docs/readme.md", "language": "unknown", "imports": []},
    ]

    graph = build_repository_graph(file_index, [])
    test_edges = [
        edge for edge in graph["edges"]
        if edge["type"] == "test_of" and edge["source"] == "tests/test_user.py"
    ]
    config_edges = [
        edge for edge in graph["edges"]
        if edge["type"] == "configures" and edge["source"] == "src/service/pyproject.toml"
    ]

    assert test_edges == [
        {
            "source_kind": "file",
            "source": "tests/test_user.py",
            "target_kind": "file",
            "target": "src/service/user.py",
            "type": "test_of",
            "confidence": pytest.approx(0.98),
            "why": "tests/test_user.py matches tests for src/service/user.py",
        }
    ]
    assert {edge["target"] for edge in config_edges} == {
        "src/service/account.py",
        "src/service/user.py",
    }


def test_repository_graph_preserves_directory_and_java_import_resolution():
    from app.tools.repository_graph import build_repository_graph

    file_index = [
        {
            "path": "src/main/java/com/example/UserService.java",
            "language": "java",
            "imports": ["com.example.User"],
        },
        {
            "path": "src/main/java/com/example/User.java",
            "language": "java",
            "imports": [],
        },
        {"path": "src/shared/account.py", "language": "python", "imports": []},
        {"path": "tests/shared/test_feature.py", "language": "python", "imports": []},
    ]

    graph = build_repository_graph(file_index, [])

    assert any(
        edge["type"] == "imports"
        and edge["source"] == "src/main/java/com/example/UserService.java"
        and edge["target"] == "src/main/java/com/example/User.java"
        for edge in graph["edges"]
    )
    assert any(
        edge["type"] == "test_of"
        and edge["source"] == "tests/shared/test_feature.py"
        and edge["target"] == "src/shared/account.py"
        for edge in graph["edges"]
    )


def test_call_graph_rejects_python_builtins_linked_to_javascript_methods():
    from app.tools.repository_graph import build_repository_graph

    files = [{"path": "model.py", "language": "python"}, {"path": "web.js", "language": "javascript"}]
    symbols = [{"file": "model.py", "symbol": "forward", "calls": ["int", "range"]},
               {"file": "web.js", "symbol": "ZodNumber.int", "calls": []},
               {"file": "web.js", "symbol": "NodePos.range", "calls": []}]
    graph = build_repository_graph(files, symbols)
    assert not [edge for edge in graph["edges"] if edge["type"] == "calls"]
    assert {symbol["id"] for symbol in graph["symbols"]} == {
        "model.py::forward", "web.js::ZodNumber.int", "web.js::NodePos.range"}
    assert symbols[0]["calls"] == ["int", "range"]


@pytest.mark.parametrize("source_language,target_language", [
    ("javascript", "typescript"), ("typescript", "javascript"), ("python", "python"), ("go", "go")])
def test_call_graph_keeps_compatible_language_calls_despite_foreign_name_collision(source_language, target_language):
    from app.tools.repository_graph import build_repository_graph

    files = [{"path": "caller", "language": source_language}, {"path": "callee", "language": target_language},
             {"path": "foreign", "language": "rust"}]
    symbols = [{"file": "caller", "symbol": "caller", "calls": ["load"]},
               {"file": "callee", "symbol": "load", "calls": []},
               {"file": "foreign", "symbol": "load", "calls": []}]
    graph = build_repository_graph(files, symbols)
    calls = [edge for edge in graph["edges"] if edge["type"] == "calls"]
    assert [(edge["source"], edge["target"]) for edge in calls] == [("caller::caller", "callee::load")]


def test_call_graph_preserves_ambiguity_and_unknown_language_without_claiming_calls():
    from app.tools.repository_graph import build_repository_graph

    files = [{"path": path, "language": language} for path, language in [
        ("caller.py", "python"), ("a.py", "python"), ("b.py", "python"), ("unknown", "unknown")]]
    symbols = [{"file": "caller.py", "symbol": "caller", "calls": ["load"]},
               {"file": "a.py", "symbol": "load", "calls": []},
               {"file": "b.py", "symbol": "load", "calls": []},
               {"file": "unknown", "symbol": "unparsed", "calls": ["caller"]}]
    graph = build_repository_graph(files, symbols)
    assert not [edge for edge in graph["edges"] if edge["type"] == "calls"]


async def test_indexer_real_mixed_files_keeps_python_calls_out_of_frontend(tmp_path):
    (tmp_path / "model.py").write_text("def forward(value):\n    return int(value) + len(list(range(3)))\n", encoding="utf-8")
    (tmp_path / "web.js").write_text("class ZodNumber { int() { return 1; } }\nclass NodePos { range() { return 1; } }\n", encoding="utf-8")
    index = await RepoIndexer().execute(repo_path=str(tmp_path))
    assert any(symbol["file"] == "model.py" and "int" in symbol["calls"] for symbol in index["symbol_index"])
    assert any(symbol["file"] == "web.js" and symbol["symbol"].endswith(".int") for symbol in index["symbol_index"])
    assert not [edge for edge in index["repository_graph"]["edges"] if edge["type"] == "calls"
                and edge.get("source_file") == "model.py" and edge.get("target_file") == "web.js"]


@pytest.mark.asyncio
async def test_scoped_index_parses_changed_file_and_direct_import_only(tmp_path, monkeypatch):
    (tmp_path / "app").mkdir()
    _write(tmp_path / "app" / "service.py",
           "from app.model import value\ndef call():\n    return value()\n")
    _write(tmp_path / "app" / "model.py", "def value():\n    return 1\n")
    _write(tmp_path / "app" / "unrelated.py", "def other():\n    return 2\n")
    calls = []
    original = default_language_registry.analyze

    def analyze(path, relative_path, **kwargs):
        calls.append(relative_path)
        return original(path, relative_path, **kwargs)

    monkeypatch.setattr(default_language_registry, "analyze", analyze)
    index = await RepoIndexer().execute(
        repo_path=str(tmp_path), changed_paths={"app/service.py"})

    assert sorted(calls) == ["app/model.py", "app/service.py"]
    files = {item["path"]: item for item in index["file_index"]}
    assert set(files) == {"app/model.py", "app/service.py", "app/unrelated.py"}
    assert files["app/unrelated.py"]["imports"] == []
    assert files["app/unrelated.py"]["parser_id"] == "unparsed"
    assert {item["file"] for item in index["symbol_index"]} == {
        "app/model.py", "app/service.py"}
    assert any(
        edge["type"] == "imports" and edge["source"] == "app/service.py"
        and edge["target"] == "app/model.py"
        for edge in index["repository_graph"]["edges"])


@pytest.mark.asyncio
async def test_blob_cache_reuses_unchanged_analysis_across_heads(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    _write(repo / "a.py", "def entry():\n    return 1\n")
    _write(repo / "b.py", "from a import entry\n")
    _git_commit(repo, "a.py", "b.py")
    first = await RepoIndexer().execute(repo_path=str(repo), changed_paths={"a.py"})

    def forbidden(*args, **kwargs):
        raise AssertionError("unchanged file was re-analyzed")

    _write(repo / "b.py", "from a import entry\nCHANGED = True\n")
    _git_commit(repo, "b.py")
    monkeypatch.setattr(isolation, "_run_worker", forbidden)
    second = await RepoIndexer().execute(repo_path=str(repo), changed_paths={"a.py"})

    assert second["file_index"][0] == first["file_index"][0]
    assert second["symbol_index"] == first["symbol_index"]


@pytest.mark.asyncio
async def test_blob_cache_invalidates_on_content_parser_and_path_change(
    tmp_path, monkeypatch,
):
    repo = _git_repo(tmp_path)
    _write(repo / "a.py", "def entry():\n    return 1\n")
    _git_commit(repo, "a.py")
    await RepoIndexer().execute(repo_path=str(repo), changed_paths={"a.py"})
    calls = []
    original = isolation._run_worker

    def analyze(request, cancel_event, **kwargs):
        calls.append(request.relative_path)
        return original(request, cancel_event, **kwargs)

    monkeypatch.setattr(isolation, "_run_worker", analyze)

    _write(repo / "a.py", "def entry():\n    return 2\n")
    _git_commit(repo, "a.py")
    await RepoIndexer().execute(repo_path=str(repo), changed_paths={"a.py"})
    assert calls == ["a.py"]

    calls.clear()
    monkeypatch.setattr("app.tools.repo_indexer.parser_fingerprint",
                        lambda: "replacement-parser")
    await RepoIndexer().execute(repo_path=str(repo), changed_paths={"a.py"})
    assert calls == ["a.py"]

    calls.clear()
    monkeypatch.setattr("app.tools.repo_indexer.ANALYSIS_CACHE_PROTOCOL", "replacement-analysis")
    await RepoIndexer().execute(repo_path=str(repo), changed_paths={"a.py"})
    assert calls == ["a.py"]

    calls.clear()
    subprocess.run(["git", "-C", str(repo), "mv", "a.py", "renamed.py"],
                   check=True, capture_output=True)
    _git_commit(repo, "renamed.py")
    await RepoIndexer().execute(repo_path=str(repo), changed_paths={"renamed.py"})
    assert calls == ["renamed.py"]


@pytest.mark.asyncio
async def test_dirty_worktree_disables_blob_cache(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    _write(repo / "a.py", "def entry():\n    return 1\n")
    _git_commit(repo, "a.py")
    await RepoIndexer().execute(repo_path=str(repo), changed_paths={"a.py"})
    _write(repo / "a.py", "def entry():\n    return 2\n")
    calls = []
    original = default_language_registry.analyze

    def analyze(path, relative_path, **kwargs):
        calls.append(relative_path)
        return original(path, relative_path, **kwargs)

    monkeypatch.setattr(default_language_registry, "analyze", analyze)
    index = await RepoIndexer().execute(repo_path=str(repo), changed_paths={"a.py"})

    assert calls == ["a.py"]
    assert index["file_index"][0]["parser_id"].startswith("tree-sitter.python")


@pytest.mark.parametrize("import_line,source", [
    ("from pkg import model", "main.py"),
    ("from . import model", "pkg/use.py"),
])
async def test_scoped_index_includes_from_import_submodules(tmp_path, import_line, source):
    (tmp_path / "pkg").mkdir()
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "model.py", "def value():\n    return 1\n")
    _write(tmp_path / source, import_line + "\ndef call():\n    return model.value()\n")

    index = await RepoIndexer().execute(repo_path=str(tmp_path), changed_paths={source})

    assert any(item["file"] == "pkg/model.py" for item in index["symbol_index"])
    assert any(edge["type"] == "calls" and edge["source_file"] == source
               and edge["target_file"] == "pkg/model.py"
               and edge["resolution_status"] == "resolved"
               for edge in index["repository_graph"]["edges"])


async def test_scoped_index_includes_neighbor_tests_and_exposes_missing_scope(tmp_path):
    (tmp_path / "tests").mkdir()
    _write(tmp_path / "service.py", "def entry():\n    return 1\n")
    _write(tmp_path / "other.py", "def other():\n    return 2\n")
    _write(tmp_path / "tests" / "test_service.py",
           "from service import entry\ndef test_entry():\n    assert entry() == 1\n")

    index = await RepoIndexer().execute(repo_path=str(tmp_path), changed_paths={"service.py"})

    assert {item["file"] for item in index["symbol_index"]} == {
        "service.py", "tests/test_service.py"}
    metadata = index["repository_graph"]["metadata"]
    assert metadata["scope_mode"] == "scoped"
    assert not metadata["scope_complete"]
    assert metadata["unparsed_file_count"] == 1
    full = await RepoIndexer().execute(repo_path=str(tmp_path))
    assert full["repository_graph"]["metadata"]["scope_complete"]


async def test_blob_cache_preserves_quarantine_failure_and_diagnostics(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    _write(repo / "a.py", "def entry():\n    return 1\n")
    _git_commit(repo, "a.py")
    await RepoIndexer().execute(repo_path=str(repo), changed_paths={"a.py"})

    def unavailable(key):
        raise isolation.ParserFailure("quarantine_unavailable")

    monkeypatch.setattr(isolation, "_load_failure", unavailable)
    index = await RepoIndexer().execute(repo_path=str(repo), changed_paths={"a.py"})

    assert index["file_index"][0]["analysis_level"] == 1
    assert "quarantine_unavailable" in index["file_index"][0]["diagnostics"][0]
    assert index["repository_graph"]["metadata"]["degraded_file_count"] == 1
    assert not index["repository_graph"]["metadata"]["scope_complete"]


@pytest.mark.parametrize("fault", ["file", "parser_id", "end_line"])
async def test_blob_cache_revalidates_identity_and_ranges(tmp_path, monkeypatch, fault):
    repo = _git_repo(tmp_path)
    _write(repo / "a.py", "def entry():\n    return 1\n")
    _git_commit(repo, "a.py")
    expected = await RepoIndexer().execute(repo_path=str(repo), changed_paths={"a.py"})
    with sqlite3.connect(parser_cache._cache_path()) as db:
        key, payload = db.execute("SELECT key, payload FROM analyses").fetchone()
        value = json.loads(payload)
        value["symbols"][0][fault] = {
            "file": "other.py", "parser_id": "invalid-parser", "end_line": 999,
        }[fault]
        raw = json.dumps(value).encode()
        db.execute("UPDATE analyses SET payload=?, digest=?, size=? WHERE key=?",
                   (raw, hashlib.sha256(raw).hexdigest(), len(raw), key))
    calls = []
    original = isolation._run_worker

    def run(request, cancel_event, **kwargs):
        calls.append(request.relative_path)
        return original(request, cancel_event, **kwargs)

    monkeypatch.setattr(isolation, "_run_worker", run)
    actual = await RepoIndexer().execute(repo_path=str(repo), changed_paths={"a.py"})

    assert calls == ["a.py"]
    assert actual == expected


async def test_blob_cache_hit_preserves_cancellation(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    _write(repo / "a.py", "def entry():\n    return 1\n")
    _git_commit(repo, "a.py")
    await RepoIndexer().execute(repo_path=str(repo), changed_paths={"a.py"})
    cancel = threading.Event()
    original = default_language_registry.analyze

    def cancelled(*args, **kwargs):
        cancel.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(default_language_registry, "analyze", cancelled)
    with pytest.raises(isolation.ParserCancelled):
        await RepoIndexer().execute(repo_path=str(repo), changed_paths={"a.py"},
                                    cancel_event=cancel)
