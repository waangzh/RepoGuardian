import pytest

from app.tools.code_search import CodeSearchTool


@pytest.mark.asyncio
async def test_find_test_files(tmp_path):
    (tmp_path / "app" / "service").mkdir(parents=True)
    (tmp_path / "app" / "service" / "user.py").write_text("def get_user(): pass")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_user.py").write_text("def test_get_user(): pass")

    file_index = [
        {"path": "app/service/user.py", "language": "python", "size": 100, "imports": []},
        {"path": "tests/test_user.py", "language": "python", "size": 200, "imports": []},
    ]
    tool = CodeSearchTool()
    snippets = await tool.retrieve_context(
        changed_files=[{"file_path": "app/service/user.py"}],
        symbol_index=[],
        file_index=file_index,
        repo_path=str(tmp_path),
    )
    assert any(s["relevance"] == "test" for s in snippets)


@pytest.mark.asyncio
async def test_retrieve_context_with_caller(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "user.py").write_text("def get_user(id):\n    return None")
    (tmp_path / "app" / "main.py").write_text("from app.user import get_user\n\ndef main():\n    get_user(1)")

    from app.tools.repo_indexer import RepoIndexer
    indexer = RepoIndexer()
    symbol_index = await indexer.build_symbol_index(str(tmp_path))

    tool = CodeSearchTool()
    snippets = await tool.retrieve_context(
        changed_files=[{"file_path": "app/user.py"}],
        symbol_index=symbol_index,
        file_index=[],
        repo_path=str(tmp_path),
    )
    # Should find direct context for get_user
    assert any(s["relevance"] == "direct" for s in snippets)
