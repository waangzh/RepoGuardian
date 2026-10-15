"""生产 Diff 内容基准回归：声明 base 只保留 PR 身份，内容基准取唯一 merge base。"""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.tools.git_tool import (
    DIFF_POLICY,
    DiffPreparationError,
    GitTool,
    GitToolError,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
         "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _forked_repo(tmp_path: Path) -> tuple[Path, str, str, str]:
    """构造 root → base 分支 / root → head 分支的真实分叉历史。"""
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    (source / "x.py").write_text('def check():\n    return "root"\n', encoding="utf-8")
    root = _commit(source, "root")
    (source / "x.py").write_text('def check():\n    return "base-branch"\n', encoding="utf-8")
    (source / "base_only.py").write_text("base_only = True\n", encoding="utf-8")
    base = _commit(source, "base")
    _git(source, "checkout", "--detach", root)
    (source / "x.py").write_text('def check():\n    return "head"\n', encoding="utf-8")
    head = _commit(source, "head")
    return source, root, base, head


def _pr(source: Path, base: str, head: str) -> SimpleNamespace:
    return SimpleNamespace(
        owner="local", repo="sample", number=1, clone_url=str(source),
        base=SimpleNamespace(sha=base, ref="main", repo_clone_url=str(source)),
        head=SimpleNamespace(sha=head, ref="feature", repo_clone_url=str(source)),
    )


def test_forked_pr_diff_excludes_base_branch_only_commits(tmp_path: Path) -> None:
    """分叉 PR 的 diff 不得包含 base 分支独有提交的反向差异。"""
    source, root, base, head = _forked_repo(tmp_path)
    tool = GitTool(tmp_path / "workspaces", command_timeout_seconds=30)

    repo_path, diff = tool.clone_and_diff(_pr(source, base, head))

    assert "-    return \"root\"" in diff and '+    return "head"' in diff
    assert "base_only.py" not in diff and "base-branch" not in diff
    assert tool.merge_base(repo_path, base, head) == root


def test_declared_base_identity_is_preserved_and_content_base_reported(tmp_path: Path) -> None:
    """声明 base 仍是 PR 身份，内容基准单独暴露给审查内容。"""
    source, root, base, head = _forked_repo(tmp_path)
    tool = GitTool(tmp_path / "workspaces", command_timeout_seconds=30)

    repo_path, _ = tool.clone_and_diff(_pr(source, base, head))
    context = tool.review_diff_context(repo_path, base, head)

    assert context == {"effective_base_sha": root, "diff_policy": DIFF_POLICY,
                       "diff_policy_version": "merge-base-v1"}
    # 声明 base 自身作为起点时内容基准就是它，策略与身份仍分别记录。
    assert tool.review_diff_context(repo_path, root, head) == {
        "effective_base_sha": root, "diff_policy": DIFF_POLICY,
        "diff_policy_version": "merge-base-v1"}


def test_linear_history_content_base_equals_declared_base(tmp_path: Path) -> None:
    """线性历史下 merge base 就是声明 base，行为与两点比较一致。"""
    source = tmp_path / "linear"
    source.mkdir()
    _git(source, "init", "-b", "main")
    (source / "x.py").write_text("x = 1\n", encoding="utf-8")
    base = _commit(source, "base")
    (source / "x.py").write_text("x = 2\n", encoding="utf-8")
    head = _commit(source, "head")
    tool = GitTool(tmp_path / "workspaces", command_timeout_seconds=30)

    repo_path, diff = tool.clone_and_diff(_pr(source, base, head))

    assert "+x = 2" in diff and "-x = 1" in diff
    assert tool.review_diff_context(repo_path, base, head)["effective_base_sha"] == base


def test_no_common_ancestor_is_rejected_not_silently_two_dot(tmp_path: Path) -> None:
    """无共同祖先时拒绝生成 diff，不退化为把全部文件当差异的两点比较。"""
    source = tmp_path / "unrelated"
    source.mkdir()
    _git(source, "init", "-b", "main")
    (source / "a.py").write_text("a = 1\n", encoding="utf-8")
    base = _commit(source, "base")
    _git(source, "checkout", "--orphan", "other")
    _git(source, "rm", "-rf", "--cached", ".")
    (source / "b.py").write_text("b = 1\n", encoding="utf-8")
    head = _commit(source, "unrelated")
    tool = GitTool(tmp_path / "workspaces", command_timeout_seconds=30)

    assert tool.merge_bases(source, base, head) == []
    with pytest.raises(DiffPreparationError, match="no_merge_base"):
        tool.unified_diff(source, base, head)
    with pytest.raises(GitToolError, match="no_merge_base"):
        tool.clone_and_diff(_pr(source, base, head))
    # 失败路径不保留任何 clone 工作区，只留空的父目录。
    assert list((tmp_path / "workspaces").iterdir()) == []


def test_multiple_merge_bases_are_rejected_with_candidates(tmp_path: Path) -> None:
    """criss-cross 历史存在多个候选时必须失败关闭，不能静默选一个 base。"""
    source = tmp_path / "criss-cross"
    source.mkdir()
    _git(source, "init", "-b", "main")
    (source / "x.py").write_text("x = 1\n", encoding="utf-8")
    root = _commit(source, "root")
    (source / "x.py").write_text("x = 2\n", encoding="utf-8")
    left = _commit(source, "left")
    _git(source, "checkout", "--detach", root)
    (source / "x.py").write_text("x = 3\n", encoding="utf-8")
    right = _commit(source, "right")
    tree = _git(source, "rev-parse", root + "^{tree}")

    def merge_commit(*parents: str) -> str:
        args = ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit-tree", tree]
        for parent in parents:
            args.extend(["-p", parent])
        return _git(source, *args, "-m", "criss-cross")

    tool = GitTool(tmp_path / "workspaces", command_timeout_seconds=30)
    left_merge, right_merge = merge_commit(left, right), merge_commit(right, left)

    bases = tool.merge_bases(source, left_merge, right_merge)
    assert sorted(bases) == sorted([left, right])
    with pytest.raises(DiffPreparationError, match="multiple_merge_bases") as excinfo:
        tool.unified_diff(source, left_merge, right_merge)
    assert sorted(excinfo.value.bases) == sorted([left, right])


def test_production_and_evaluation_share_one_diff_function(tmp_path: Path) -> None:
    """生产 GitTool 与评测 SnapshotGitTool 必须给出同一基准与同一 diff 字节。"""
    from app.evaluation.runner import SnapshotGitTool
    from app.tools.git_tool import DIFF_CONTEXT_LINES, DIFF_POLICY_VERSION

    source, root, base, head = _forked_repo(tmp_path)
    pr = _pr(source, base, head)
    production = GitTool(tmp_path / "production", command_timeout_seconds=30)
    production_repo, production_diff = production.clone_and_diff(pr)
    snapshot = SnapshotGitTool(tmp_path / "evaluation", command_timeout_seconds=30)
    snapshot_repo, snapshot_diff = snapshot.clone_and_diff(pr)

    assert production_diff == snapshot_diff
    assert production.merge_base(production_repo, base, head) == root
    assert snapshot.merge_base(snapshot_repo, base, head) == root
    context = production.review_diff_context(production_repo, base, head)
    assert context["effective_base_sha"] == root
    assert snapshot.review_diff_context(snapshot_repo, base, head) == context
    assert context["diff_policy_version"] == DIFF_POLICY_VERSION == "merge-base-v1"
    # 断言与生成共用同一上下文行数常量，避免两侧策略版本漂移。
    expected = production._run(["git", "-C", str(production_repo), "diff", "--no-ext-diff",
                                f"--unified={DIFF_CONTEXT_LINES}", root, head])
    assert expected == production_diff
