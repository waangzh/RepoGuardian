import threading

import pytest

from app.tools.repo_indexer import RepoIndexer


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
async def test_project_meta_exposes_language_analysis_capabilities(tmp_path):
    (tmp_path / "main.ts").write_text("export const main = () => true\n", encoding="utf-8")
    (tmp_path / "legacy.java").write_text("class Legacy {}\n", encoding="utf-8")

    indexer = RepoIndexer()
    file_index = await indexer.build_file_index(str(tmp_path))
    meta = await indexer.detect_project_meta(str(tmp_path), file_index)
    capabilities = {item["language_id"]: item for item in meta["analysis_capabilities"]}

    assert capabilities["typescript"]["max_level"] == 2
    assert capabilities["java"]["max_level"] == 1


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
        return [{"path": "app.py", "language": "python", "imports": []}]

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
