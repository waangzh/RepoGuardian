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
                       "diff_policy_version": "merge-base-v2"}
    # 声明 base 自身作为起点时内容基准就是它，策略与身份仍分别记录。
    assert tool.review_diff_context(repo_path, root, head) == {
        "effective_base_sha": root, "diff_policy": DIFF_POLICY,
        "diff_policy_version": "merge-base-v2"}


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
    assert context["diff_policy_version"] == DIFF_POLICY_VERSION == "merge-base-v2"
    # 断言与生成共用同一上下文行数常量，避免两侧策略版本漂移。
    expected = production._run(["git", "-C", str(production_repo), "diff", "--no-ext-diff",
                                f"--unified={DIFF_CONTEXT_LINES}", root, head])
    assert expected == production_diff


def test_u3_splits_hunks_without_losing_changed_lines(tmp_path: Path) -> None:
    from app.tools.diff_parser import DiffParser
    from app.tools.git_tool import DIFF_CONTEXT_LINES

    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    lines = [f"value_{index} = {index}\n" for index in range(200)]
    (source / "x.py").write_text("".join(lines), encoding="utf-8")
    base = _commit(source, "base")
    lines[60] = "value_60 = updated(60)\n"
    lines[120] = "value_120 = updated(120)\n"
    (source / "x.py").write_text("".join(lines), encoding="utf-8")
    head = _commit(source, "head")
    tool = GitTool(tmp_path / "workspaces", command_timeout_seconds=30)
    effective, compact = tool.unified_diff(source, base, head)
    wide = _git(source, "diff", "--no-ext-diff", "--unified=80", base, head)
    compact_file, wide_file = DiffParser().parse(compact)[0], DiffParser().parse(wide + "\n")[0]
    assert DIFF_CONTEXT_LINES == 3 and effective == base
    assert len(compact_file.hunks) == 2 and len(wide_file.hunks) == 1
    assert (compact_file.additions, compact_file.deletions) == (2, 2)

    def changes(file):
        return [line.model_dump(mode="json") for hunk in file.hunks
                for line in hunk.lines if line.kind != "context"]

    assert changes(compact_file) == changes(wide_file)
    assert len(compact) < len(wide)
    assert all(sum(line.kind == "context" for line in hunk.lines) == 6
               for hunk in compact_file.hunks)


def _head_and_base_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """构造 base → head 的线性历史，返回 (source, base, head)。"""
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    (source / "x.py").write_text("x = 1\n", encoding="utf-8")
    base = _commit(source, "base")
    (source / "x.py").write_text("x = 2\n", encoding="utf-8")
    head = _commit(source, "head")
    return source, base, head


def _pr_with_heads(source: Path, base: str, head: str, base_sha=None, head_sha=None):
    base_sha = base_sha if base_sha is not None else base
    head_sha = head_sha if head_sha is not None else head
    return SimpleNamespace(
        owner="local", repo="sample", number=1, clone_url=str(source),
        base=SimpleNamespace(sha=base_sha, ref="main", repo_clone_url=str(source)),
        head=SimpleNamespace(sha=head_sha, ref="main", repo_clone_url=str(source)),
    )


def test_missing_declared_head_fails_instead_of_reviewing_another_commit(tmp_path: Path) -> None:
    """声明的 head 无法获得时明确失败，禁止降级后按分支新 tip 继续审查。"""
    source, base, head = _head_and_base_repo(tmp_path)
    # 该提交在远端与本地对象库都不存在：按 SHA fetch 失败，按分支名 fetch 只能取回当前 tip。
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _git(elsewhere, "init", "-b", "main")
    (elsewhere / "y.py").write_text("y = 1\n", encoding="utf-8")
    missing_head = _commit(elsewhere, "unrelated")
    assert missing_head != head

    tool = GitTool(tmp_path / "workspaces", command_timeout_seconds=30)
    with pytest.raises(GitToolError, match="缺少请求的提交"):
        tool.clone_and_diff(_pr_with_heads(source, base, head, head_sha=missing_head))
    # 失败路径不保留工作区，也没有把另一个提交当成本次快照。
    assert list((tmp_path / "workspaces").iterdir()) == []


def test_fetch_ref_uses_local_commit_without_branch_fallback(tmp_path: Path) -> None:
    """请求的提交已在本地对象库时直接使用，不因远端不可用而降级到分支名。"""
    source, base, head = _head_and_base_repo(tmp_path)
    tool = GitTool(tmp_path / "workspaces", command_timeout_seconds=30)
    repo_path, _ = tool.clone_and_diff(_pr_with_heads(source, base, head))

    # 远端不可达：按 SHA fetch 必然失败，但请求的提交已在本地，不得再尝试分支名。
    tool._fetch_ref(
        repo_path, str(tmp_path / "missing-remote"), head, "main",
        phase="fetch_head", phase_label="正在获取 Head 分支",
        progress_callback=None, cancel_event=None,
    )
    # 本地也不存在该提交时，同样不降级到分支名（远端不可用），必须失败。
    with pytest.raises(GitToolError):
        tool._fetch_ref(
            repo_path, str(tmp_path / "missing-remote"), "f" * 40, "missing-branch",
            phase="fetch_head", phase_label="正在获取 Head 分支",
            progress_callback=None, cancel_event=None,
        )


def test_branch_fallback_identity_follows_declared_sha_not_fetch_head(tmp_path: Path) -> None:
    """分支降级取回后的身份由声明 SHA 决定，FETCH_HEAD 指向分支新 tip 也不改变快照。"""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-b", "main")
    (upstream / "x.py").write_text("x = 1\n", encoding="utf-8")
    base = _commit(upstream, "base")

    # fork 的 feature 分支：声明的 head 之后又前进了一个提交，两者都只存在于 fork。
    fork = tmp_path / "fork"
    _git(tmp_path, "clone", "--no-checkout", str(upstream), str(fork))
    _git(fork, "checkout", "-q", "-b", "feature")
    (fork / "x.py").write_text("x = 2\n", encoding="utf-8")
    head = _commit(fork, "head")
    (fork / "x.py").write_text("x = 3\n", encoding="utf-8")
    newer = _commit(fork, "head moved on")
    assert newer != head

    class ShaFetchRejected(GitTool):
        """模拟远端拒绝按 SHA 获取：声明 head 只随分支名进入本地。"""

        def _run(self, command, **kwargs):
            if "fetch" in command and command[-1] == head:
                raise GitToolError("simulated: server rejects fetch by sha")
            return super()._run(command, **kwargs)

    pr = SimpleNamespace(
        owner="local", repo="sample", number=1, clone_url=str(upstream),
        base=SimpleNamespace(sha=base, ref="main", repo_clone_url=str(upstream)),
        head=SimpleNamespace(sha=head, ref="feature", repo_clone_url=str(fork)),
    )
    tool = ShaFetchRejected(tmp_path / "workspaces", command_timeout_seconds=30)
    repo_path, diff = tool.clone_and_diff(pr)

    # 降级 fetch 成功，但 FETCH_HEAD 是分支新 tip；身份必须仍是声明的 head。
    assert tool._run(["git", "-C", str(repo_path), "rev-parse", "FETCH_HEAD"]).strip() == newer
    assert tool._run(["git", "-C", str(repo_path), "rev-parse", "HEAD"]).strip() == head
    assert "+x = 2" in diff and "+x = 3" not in diff


def test_clone_and_diff_rejects_declared_sha_that_is_not_a_commit(tmp_path: Path) -> None:
    """声明字段不是完整提交 ID 时在 clone 前失败，不产生工作区副作用。"""
    source, base, head = _head_and_base_repo(tmp_path)
    workspaces = tmp_path / "workspaces"

    for field, value, expected in [
        ("head", "feature", "PR head"),
        ("base", base[:12], "PR base"),
    ]:
        pull_request = _pr_with_heads(source, base, head)
        setattr(pull_request, field, SimpleNamespace(sha=value, ref="main",
                                                    repo_clone_url=str(source)))
        tool = GitTool(workspaces, command_timeout_seconds=30)
        with pytest.raises(GitToolError, match=expected):
            tool.clone_and_diff(pull_request)
    assert not workspaces.exists()
