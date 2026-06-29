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
