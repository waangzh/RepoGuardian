import os
import subprocess
from pathlib import Path

import pytest

from app.review.tool_scope import ReviewPathPolicyError, validate_repository_file
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

    from app.review.retrieval import search
    from types import SimpleNamespace
    evidence = SimpleNamespace(allowed_paths=frozenset({".env"}), _source=lambda *args: (_ for _ in ()).throw(ValueError("sensitive_path")))
    context = SimpleNamespace(evidence=evidence, denied_paths=frozenset(), cursors={}, check_active=None)
    with pytest.raises(ValueError, match="path_not_authorized"):
        await search(context, "TOKEN", path=".env")
