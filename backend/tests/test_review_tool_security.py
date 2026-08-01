import os
import subprocess
from pathlib import Path

import pytest

from app.models.review import ReviewToolScope
from app.review.tool_scope import ReviewPathPolicyError, validate_repository_file
from app.tools.code_search import CodeSearchTool, ContextRetrievalPlanError
from app.tools.repo_indexer import RepoIndexer


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    (repo / "safe.py").write_text("SAFE = True\n", encoding="utf-8")
    (repo / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    subprocess.run(["git", "add", "safe.py", ".env"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=test@example.test", "-c", "user.name=Test",
         "commit", "-m", "fixture"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "untracked.py").write_text("UNTRACKED = True\n", encoding="utf-8")
    return repo


def test_sensitive_and_untracked_paths_are_rejected(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)

    with pytest.raises(ReviewPathPolicyError, match="sensitive"):
        validate_repository_file(repo, ".env")
    with pytest.raises(ReviewPathPolicyError, match="not Git tracked"):
        validate_repository_file(repo, "untracked.py")
    assert validate_repository_file(repo, "safe.py") == (repo / "safe.py").resolve()


def test_realpath_escape_is_rejected(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("SECRET = True\n", encoding="utf-8")
    link = repo / "escape.py"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("当前 Windows 环境不允许创建符号链接")
    subprocess.run(["git", "add", "escape.py"], cwd=repo, check=True, capture_output=True)

    with pytest.raises(ReviewPathPolicyError, match="escapes repository root"):
        validate_repository_file(repo, "escape.py")


@pytest.mark.asyncio
async def test_code_search_and_indexer_fail_closed_for_sensitive_files(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    index = await RepoIndexer().build_file_index(str(repo))
    assert {item["path"] for item in index} == {"safe.py"}

    scope = ReviewToolScope(
        review_unit_id="unit",
        commentable_files={".env"},
        readable_files={".env"},
        repository_root=str(repo),
        max_lines_per_read=20,
        max_search_results=4,
    )
    with pytest.raises(ContextRetrievalPlanError, match="sensitive"):
        await CodeSearchTool().retrieve_context(
            changed_files=[{"file_path": ".env"}],
            symbol_index=[],
            file_index=[{"path": ".env"}],
            repo_path=str(repo),
            plan={
                "reason": "尝试读取敏感文件",
                "target_files": [".env"],
                "search_terms": ["TOKEN"],
                "relevance_types": ["text"],
            },
            scope=scope,
        )
