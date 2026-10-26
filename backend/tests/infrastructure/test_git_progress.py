import sys
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.tools.git_tool import (
    GitProgressEvent,
    GitTool,
    GitToolCancelled,
    GitToolTimeout,
    _parse_git_progress_line,
)


def test_parse_git_progress_line_extracts_real_git_metrics() -> None:
    event = _parse_git_progress_line(
        "remote: Receiving objects: 42% (1,240/2,950), 38.20 MiB | 4.10 MiB/s",
        "clone",
        "正在克隆仓库",
    )

    assert event == GitProgressEvent(
        phase="clone",
        operation="receiving_objects",
        message="正在克隆仓库：接收对象 42%",
        percent=42,
        current=1240,
        total=2950,
        detail="38.20 MiB | 4.10 MiB/s",
    )


def test_run_streams_progress_without_waiting_for_process_exit(tmp_path: Path) -> None:
    tool = GitTool(tmp_path, command_timeout_seconds=5)
    events: list[GitProgressEvent] = []
    script = (
        "import sys,time; "
        "sys.stderr.write('Receiving objects: 42% (42/100), 1.00 MiB | 2.00 MiB/s\\r'); "
        "sys.stderr.flush(); time.sleep(0.05); "
        "sys.stderr.write('Receiving objects: 100% (100/100), 2.00 MiB | 3.00 MiB/s, done.\\n'); "
        "sys.stderr.flush(); print('ok')"
    )

    output = tool._run(
        [sys.executable, "-c", script],
        progress_callback=events.append,
        phase="clone",
        phase_label="正在克隆仓库",
    )

    assert output.strip() == "ok"
    assert [event.percent for event in events] == [42, 100]
    assert events[-1].detail == "2.00 MiB | 3.00 MiB/s"


def test_run_terminates_process_when_cancelled(tmp_path: Path) -> None:
    tool = GitTool(tmp_path, command_timeout_seconds=5)
    cancel_event = threading.Event()
    timer = threading.Timer(0.15, cancel_event.set)
    started_at = time.monotonic()
    timer.start()
    try:
        with pytest.raises(GitToolCancelled, match="已取消"):
            tool._run(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                cancel_event=cancel_event,
            )
    finally:
        timer.cancel()

    assert time.monotonic() - started_at < 3


def test_run_terminates_process_after_timeout(tmp_path: Path) -> None:
    tool = GitTool(tmp_path, command_timeout_seconds=0.15)
    started_at = time.monotonic()

    with pytest.raises(GitToolTimeout, match="超时"):
        tool._run([sys.executable, "-c", "import time; time.sleep(5)"])

    assert time.monotonic() - started_at < 3


def test_clone_and_diff_reports_all_repository_preparation_phases(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()

    def git(*args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=source,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return completed.stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "tests@example.test")
    git("config", "user.name", "RepoGuardian Tests")
    (source / "sample.py").write_text("value = 'base'\n", encoding="utf-8")
    git("add", "sample.py")
    git("commit", "-m", "base")
    base_sha = git("rev-parse", "HEAD")
    git("checkout", "-b", "feature")
    (source / "sample.py").write_text("value = 'head'\n", encoding="utf-8")
    git("commit", "-am", "head")
    head_sha = git("rev-parse", "HEAD")

    events: list[GitProgressEvent] = []
    workspaces = tmp_path / "workspaces"
    tool = GitTool(workspaces, command_timeout_seconds=10)
    pr = SimpleNamespace(
        owner="local",
        repo="sample",
        number=1,
        clone_url=str(source),
        base=SimpleNamespace(sha=base_sha, ref="main"),
        head=SimpleNamespace(sha=head_sha, ref="feature", repo_clone_url=str(source)),
    )

    repo_path, diff = tool.clone_and_diff(pr, progress_callback=events.append)

    assert repo_path.is_dir()
    assert "+value = 'head'" in diff
    assert {event.phase for event in events} == {
        "clone",
        "fetch_base",
        "fetch_head",
        "diff",
        "checkout",
    }
    assert events[-1] == GitProgressEvent(
        phase="checkout",
        operation="completed",
        message="Head 工作树已就绪",
        percent=100,
    )
