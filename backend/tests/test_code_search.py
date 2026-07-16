import pytest

from app.models.review import ContextRetrievalPlan
from app.tools.code_search import CodeSearchTool, ContextRetrievalPlanError


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


@pytest.mark.asyncio
async def test_structured_plan_retrieves_callers_callees_and_tests(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "app" / "service.py").write_text(
        "def leaf():\n    return 1\n\n"
        "def root():\n    return leaf()\n\n"
        "def caller():\n    return root()\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_service.py").write_text(
        "from app.service import root\n\n\ndef test_root():\n    assert root() == 1\n",
        encoding="utf-8",
    )
    from app.tools.repo_indexer import RepoIndexer

    indexer = RepoIndexer()
    file_index = await indexer.build_file_index(str(tmp_path))
    symbol_index = await indexer.build_symbol_index(str(tmp_path))
    plan = ContextRetrievalPlan.model_validate({
        "reason": "确认 root 的依赖与覆盖。",
        "target_files": ["app/service.py"],
        "target_symbols": ["root"],
        "search_terms": [],
        "relevance_types": ["direct"],
        "include_callers": True,
        "include_callees": True,
        "include_tests": True,
        "max_results": 12,
        "depth": 1,
    })

    snippets = await CodeSearchTool().retrieve_context(
        changed_files=[],
        symbol_index=symbol_index,
        file_index=file_index,
        repo_path=str(tmp_path),
        plan=plan,
    )

    assert any(item["relevance"] == "caller" and item["symbol"] == "caller" for item in snippets)
    assert any(item["relevance"] == "callee" and item["symbol"] == "leaf" for item in snippets)
    assert any(item["relevance"] == "test" and item["file"] == "tests/test_service.py" for item in snippets)


@pytest.mark.asyncio
async def test_plan_rejects_unindexed_file_and_clamps_bounds(tmp_path):
    (tmp_path / "app.py").write_text("\n".join(f"def f{i}(): pass" for i in range(30)), encoding="utf-8")
    from app.tools.repo_indexer import RepoIndexer

    indexer = RepoIndexer()
    file_index = await indexer.build_file_index(str(tmp_path))
    symbol_index = await indexer.build_symbol_index(str(tmp_path))
    unsafe_plan = ContextRetrievalPlan.model_validate({
        "reason": "越权文件。",
        "target_files": ["other.py"],
        "target_symbols": [],
        "search_terms": [],
        "relevance_types": ["direct"],
        "include_callers": False,
        "include_callees": False,
        "include_tests": False,
        "max_results": 99,
        "depth": 99,
    })
    assert unsafe_plan.max_results == 20
    assert unsafe_plan.depth == 2

    with pytest.raises(ContextRetrievalPlanError, match="not in repository index"):
        await CodeSearchTool().retrieve_context(
            changed_files=[],
            symbol_index=symbol_index,
            file_index=file_index,
            repo_path=str(tmp_path),
            plan=unsafe_plan,
        )

    bounded_plan = unsafe_plan.model_copy(update={"target_files": ["app.py"]})
    snippets = await CodeSearchTool().retrieve_context(
        changed_files=[],
        symbol_index=symbol_index,
        file_index=file_index,
        repo_path=str(tmp_path),
        plan=bounded_plan,
    )
    assert len(snippets) == 20
