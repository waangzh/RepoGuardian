import os
import stat
from pathlib import Path
from types import SimpleNamespace

from app.tools.git_tool import GitTool, GitToolError
from app.tools.workspace_cleanup import cleanup_workspace, reap_orphaned_workspaces


def test_cleanup_workspace_removes_readonly_git_files(tmp_path: Path) -> None:
    workdir = tmp_path / "workspaces"
    workspace = workdir / "repo-1"
    git_object = workspace / ".git" / "objects" / "pack" / "pack-test.pack"
    git_object.parent.mkdir(parents=True)
    git_object.write_bytes(b"git object")
    git_object.chmod(stat.S_IREAD)

    assert cleanup_workspace(workspace, workdir=workdir)
    assert not workspace.exists()


def test_cleanup_workspace_rejects_root_and_outside_paths(tmp_path: Path) -> None:
    workdir = tmp_path / "workspaces"
    workdir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    assert cleanup_workspace(workdir, workdir=workdir) is False
    assert cleanup_workspace(outside, workdir=workdir) is False
    assert workdir.exists()
    assert outside.exists()


def test_reaper_only_removes_old_inactive_workspaces(tmp_path: Path) -> None:
    workdir = tmp_path / "workspaces"
    old = workdir / "old"
    active = workdir / "active"
    recent = workdir / "recent"
    for path in (old, active, recent):
        path.mkdir(parents=True)
        (path / "file.txt").write_text("content", encoding="utf-8")
    os.utime(old, (100.0, 100.0))
    os.utime(active, (100.0, 100.0))
    os.utime(recent, (950.0, 950.0))

    result = reap_orphaned_workspaces(
        workdir=workdir,
        older_than_seconds=100.0,
        active_paths=[active],
        now=1_000.0,
    )

    assert result.removed == 1
    assert result.failed == 0
    assert not old.exists()
    assert active.exists()
    assert recent.exists()


def test_clone_failure_cleans_partially_created_workspace(tmp_path: Path) -> None:
    tool = GitTool(tmp_path)

    def fail_clone(command: list[str]) -> str:
        repo_dir = Path(command[-1])
        repo_dir.mkdir(parents=True)
        partial = repo_dir / "partial.pack"
        partial.write_bytes(b"partial")
        partial.chmod(stat.S_IREAD)
        raise GitToolError("clone failed")

    tool._run = fail_clone  # type: ignore[method-assign]
    pr = SimpleNamespace(
        owner="acme",
        repo="repo",
        number=7,
        clone_url="https://example.invalid/acme/repo.git",
        base=SimpleNamespace(sha="a" * 40, ref="main"),
        head=SimpleNamespace(
            sha="b" * 40,
            ref="feature",
            repo_clone_url="https://example.invalid/acme/repo.git",
        ),
    )

    try:
        tool.clone_and_diff(pr)
    except GitToolError:
        pass
    else:
        raise AssertionError("clone_and_diff should propagate GitToolError")

    assert list(tmp_path.iterdir()) == []
