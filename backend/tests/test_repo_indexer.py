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
