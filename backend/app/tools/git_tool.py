"""Git 操作工具 —— 克隆仓库、生成 diff、读取文件内容。"""

import logging
import queue
import re
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO, Callable
from uuid import uuid4

from app.models.review import PullRequestInfo
from app.review.tool_scope import (
    ReviewPathPolicyError,
    list_git_tracked_files,
    validate_repository_file,
)
from app.tools.workspace_cleanup import cleanup_workspace

logger = logging.getLogger("RepoGuardian.GitTool")

_GIT_PROGRESS_RE = re.compile(
    r"(?:remote:\s*)?"
    r"(?P<operation>Counting objects|Compressing objects|Receiving objects|"
    r"Resolving deltas|Updating files):\s*"
    r"(?P<percent>\d{1,3})%\s*"
    r"\((?P<current>[\d,]+)/(?P<total>[\d,]+)\)"
)
_OPERATION_LABELS = {
    "Counting objects": "统计对象",
    "Compressing objects": "压缩对象",
    "Receiving objects": "接收对象",
    "Resolving deltas": "解析增量",
    "Updating files": "更新工作树",
}
_OPERATION_NAMES = {
    "Counting objects": "counting_objects",
    "Compressing objects": "compressing_objects",
    "Receiving objects": "receiving_objects",
    "Resolving deltas": "resolving_deltas",
    "Updating files": "updating_files",
}


@dataclass(frozen=True)
class GitProgressEvent:
    """单个 Git 阶段的可序列化进度快照。"""

    phase: str
    message: str
    operation: str | None = None
    percent: int | None = None
    current: int | None = None
    total: int | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, str | int | None]:
        return asdict(self)


GitProgressCallback = Callable[[GitProgressEvent], None]


class GitToolError(RuntimeError):
    """Git 命令执行失败时抛出。"""
    pass


class GitToolCancelled(GitToolError):
    """Git 命令被任务取消。"""


class GitToolTimeout(GitToolError):
    """Git 命令超过允许执行时间。"""


class GitTool:
    """在临时目录中执行 Git 操作：克隆、fetch refs、生成 unified diff。

    克隆到 settings.repoguardian_workdir 下随机命名的子目录中，
    避免并发任务冲突。
    """

    def __init__(
        self,
        workdir: Path | None = None,
        git_executable: str = "git",
        command_timeout_seconds: float | None = None,
    ) -> None:
        from app.core.config import settings
        self._workdir = workdir or settings.repoguardian_workdir
        self._git = git_executable
        self._command_timeout_seconds = (
            command_timeout_seconds
            if command_timeout_seconds is not None
            else settings.repoguardian_git_timeout_seconds
        )

    def get_file_content(
        self, repo_path: str | Path, file_path: str, start_line: int = 1, end_line: int | None = None
    ) -> str:
        """从检出的仓库中直接读取指定文件的指定行范围（非 git 命令，直接文件 I/O）。"""
        try:
            full_path = validate_repository_file(repo_path, file_path)
        except (OSError, ReviewPathPolicyError):
            return ""
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace", newline="") as f:
                lines = f.readlines()
        except (OSError, UnicodeDecodeError):
            return ""
        if end_line is None:
            end_line = len(lines)
        start_idx = max(start_line - 1, 0)
        end_idx = min(end_line, len(lines))
        return "".join(lines[start_idx:end_idx])

    def list_tracked_files(self, repo_path: str | Path) -> set[str]:
        """列出仓库索引中的文件，供 RepoIndexer 和只读工具共享。"""
        return list_git_tracked_files(repo_path, self._git)

    def get_file_content_at_revision(
        self, repo_path: str | Path, revision: str, file_path: str
    ) -> str | None:
        """读取服务端选定 revision 的文件，不切换工作树。"""
        repo_dir = self._validate_worktree(repo_path)
        completed = subprocess.run(
            [self._git, "-C", str(repo_dir), "show", f"{revision}:{file_path}"],
            check=False,
            capture_output=True,
        )
        if completed.returncode != 0 or b"\x00" in completed.stdout:
            return None
        return completed.stdout.decode("utf-8", errors="replace")

    def clone_and_diff(
        self,
        pr: PullRequestInfo,
        *,
        progress_callback: GitProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> tuple[Path, str]:
        """核心操作：clone → fetch base/head refs → 生成 unified diff → checkout head。

        返回 (仓库临时路径, diff 文本)。
        """
        self._workdir.mkdir(parents=True, exist_ok=True)
        repo_dir = self._workdir / f"{pr.owner}-{pr.repo}-{pr.number}-{uuid4().hex[:8]}"
        try:
            # 克隆时不检出工作树；--progress 强制非 TTY 环境也输出结构化进度。
            self._notify(progress_callback, "clone", "正在克隆仓库")
            self._run(
                [self._git, "clone", "--progress", "--no-checkout", pr.clone_url, str(repo_dir)],
                progress_callback=progress_callback,
                phase="clone",
                phase_label="正在克隆仓库",
                cancel_event=cancel_event,
            )
            self._notify(progress_callback, "clone", "仓库克隆完成", "completed", 100)
            # 分别 fetch base 和 head 的 SHA
            self._fetch_ref(
                repo_dir,
                "origin",
                pr.base.sha,
                pr.base.ref,
                phase="fetch_base",
                phase_label="正在获取 Base 分支",
                progress_callback=progress_callback,
                cancel_event=cancel_event,
            )
            self._fetch_ref(
                repo_dir,
                pr.head.repo_clone_url,
                pr.head.sha,
                pr.head.ref,
                phase="fetch_head",
                phase_label="正在获取 Head 分支",
                progress_callback=progress_callback,
                cancel_event=cancel_event,
            )
            # 生成 unified diff（上下文 80 行，足够 LLM 理解）
            self._notify(progress_callback, "diff", "正在生成 PR Diff")
            diff = self._run(
                [self._git, "-C", str(repo_dir), "diff", "--unified=80", pr.base.sha, "FETCH_HEAD"],
                cancel_event=cancel_event,
            )
            self._notify(progress_callback, "diff", "PR Diff 已生成", "completed", 100)
            # 检出 head 到工作树（后续静态分析/测试需要）
            self._notify(progress_callback, "checkout", "正在检出 Head 工作树")
            self._run(
                [self._git, "-C", str(repo_dir), "checkout", "--progress", "--detach", "FETCH_HEAD"],
                progress_callback=progress_callback,
                phase="checkout",
                phase_label="正在检出 Head 工作树",
                cancel_event=cancel_event,
            )
            self._notify(progress_callback, "checkout", "Head 工作树已就绪", "completed", 100)
            return repo_dir, diff
        except BaseException:
            cleanup_workspace(repo_dir, workdir=self._workdir)
            raise

    def checkout_sha(self, repo_path: str | Path, sha: str) -> None:
        """在任务临时 clone 中切换到已 fetch 的确定 SHA。"""
        repo_dir = Path(repo_path).resolve()
        self._run([self._git, "-C", str(repo_dir), "checkout", "--detach", sha])

    def reset_to_sha(self, repo_path: str | Path, sha: str) -> None:
        """强制将任务临时 clone 复位到一个服务端已获取的确定 SHA。"""
        repo_dir = self._validate_worktree(repo_path)
        self._run([self._git, "-C", str(repo_dir), "reset", "--hard", sha])

    def clean_worktree(self, repo_path: str | Path) -> None:
        """删除临时 clone 中所有未跟踪和忽略文件，不保留候选补丁副作用。"""
        repo_dir = self._validate_worktree(repo_path)
        self._run([self._git, "-C", str(repo_dir), "clean", "-fdx"])

    def prepare_patch_workspace(self, repo_path: str | Path, head_sha: str) -> None:
        """为单个候选补丁建立 ``Head + 当前补丁`` 的唯一允许验证起点。"""
        self.reset_to_sha(repo_path, head_sha)
        self.clean_worktree(repo_path)

    @staticmethod
    def _validate_worktree(repo_path: str | Path) -> Path:
        repo_dir = Path(repo_path).resolve()
        if not repo_dir.is_dir() or not (repo_dir / ".git").exists():
            raise GitToolError(f"Repository path is not a git worktree: {repo_dir}")
        return repo_dir

    def _run(
        self,
        command: list[str],
        *,
        progress_callback: GitProgressCallback | None = None,
        phase: str | None = None,
        phase_label: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> str:
        """执行参数化 Git 命令，同时排空双管道并解析 stderr 中的进度。"""
        if cancel_event is not None and cancel_event.is_set():
            raise GitToolCancelled("Git 操作已取消")
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise GitToolError(f"无法启动 Git 命令：{exc}") from exc

        assert process.stdout is not None
        assert process.stderr is not None
        output_queue: queue.Queue[tuple[str, bytes | None]] = queue.Queue()
        readers = [
            threading.Thread(
                target=_pump_stream,
                args=("stdout", process.stdout, output_queue),
                daemon=True,
            ),
            threading.Thread(
                target=_pump_stream,
                args=("stderr", process.stderr, output_queue),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()

        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        stderr_buffer = ""
        remaining_streams = {"stdout", "stderr"}
        started_at = time.monotonic()
        stop_reason: str | None = None
        last_progress_key: tuple[str, int] | None = None
        last_progress_at = 0.0

        while remaining_streams:
            if stop_reason is None and cancel_event is not None and cancel_event.is_set():
                stop_reason = "Git 操作已取消"
                _terminate_process(process)
            elif (
                stop_reason is None
                and self._command_timeout_seconds > 0
                and time.monotonic() - started_at > self._command_timeout_seconds
            ):
                stop_reason = f"Git 操作超过 {self._command_timeout_seconds:g} 秒超时"
                _terminate_process(process)

            try:
                stream_name, chunk = output_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if chunk is None:
                remaining_streams.discard(stream_name)
                continue
            if stream_name == "stdout":
                stdout_chunks.append(chunk)
                continue

            stderr_chunks.append(chunk)
            if progress_callback is None or phase is None or phase_label is None:
                continue
            stderr_buffer += chunk.decode("utf-8", errors="replace")
            lines = re.split(r"[\r\n]+", stderr_buffer)
            stderr_buffer = lines.pop()
            for line in lines:
                event = _parse_git_progress_line(line, phase, phase_label)
                if event is None or event.operation is None or event.percent is None:
                    continue
                now = time.monotonic()
                key = (event.operation, event.percent)
                operation_changed = last_progress_key is None or key[0] != last_progress_key[0]
                if operation_changed or event.percent == 100 or now - last_progress_at >= 0.25:
                    self._emit(progress_callback, event)
                    last_progress_key = key
                    last_progress_at = now

        if stderr_buffer and progress_callback and phase and phase_label:
            event = _parse_git_progress_line(stderr_buffer, phase, phase_label)
            if event is not None:
                self._emit(progress_callback, event)
        for reader in readers:
            reader.join(timeout=1.0)
        return_code = process.wait()
        stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace")
        stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        if stop_reason is not None:
            if cancel_event is not None and cancel_event.is_set():
                raise GitToolCancelled(stop_reason)
            raise GitToolTimeout(stop_reason)
        if return_code != 0:
            detail = (stderr or stdout).strip()[-8000:]
            raise GitToolError(f"Git 命令执行失败（exit {return_code}）\n{detail}")
        return stdout

    def _fetch_ref(
        self,
        repo_dir: Path,
        remote: str,
        sha: str,
        ref: str,
        *,
        phase: str,
        phase_label: str,
        progress_callback: GitProgressCallback | None,
        cancel_event: threading.Event | None,
    ) -> None:
        """拉取指定 SHA，失败时回退到 ref 名称重试。"""
        self._notify(progress_callback, phase, phase_label)
        command = [self._git, "-C", str(repo_dir), "fetch", "--progress", remote, sha]
        try:
            self._run(
                command,
                progress_callback=progress_callback,
                phase=phase,
                phase_label=phase_label,
                cancel_event=cancel_event,
            )
        except GitToolError as primary_error:
            if isinstance(primary_error, (GitToolCancelled, GitToolTimeout)):
                raise
            self._notify(progress_callback, phase, f"{phase_label}：按分支名重试")
            fallback = [self._git, "-C", str(repo_dir), "fetch", "--progress", remote, ref]
            try:
                self._run(
                    fallback,
                    progress_callback=progress_callback,
                    phase=phase,
                    phase_label=phase_label,
                    cancel_event=cancel_event,
                )
            except GitToolError as fallback_error:
                raise GitToolError(
                    f"按 SHA 和分支名获取 {phase} 均失败：{fallback_error}"
                ) from primary_error
        self._notify(
            progress_callback,
            phase,
            f"{phase_label.removeprefix('正在')}完成",
            "completed",
            100,
        )

    @staticmethod
    def _emit(callback: GitProgressCallback | None, event: GitProgressEvent) -> None:
        if callback is None:
            return
        try:
            callback(event)
        except Exception:
            logger.warning("Git 进度回调失败，继续执行 Git 命令", exc_info=True)

    @classmethod
    def _notify(
        cls,
        callback: GitProgressCallback | None,
        phase: str,
        message: str,
        operation: str | None = None,
        percent: int | None = None,
    ) -> None:
        cls._emit(
            callback,
            GitProgressEvent(
                phase=phase,
                message=message,
                operation=operation,
                percent=percent,
            ),
        )


def _parse_git_progress_line(
    line: str,
    phase: str,
    phase_label: str,
) -> GitProgressEvent | None:
    match = _GIT_PROGRESS_RE.search(line)
    if match is None:
        return None
    raw_operation = match.group("operation")
    percent = min(100, max(0, int(match.group("percent"))))
    detail = line[match.end():].strip(" ,")
    detail = re.sub(r"(?:,\s*)?done\.?$", "", detail, flags=re.IGNORECASE).strip(" ,")
    return GitProgressEvent(
        phase=phase,
        operation=_OPERATION_NAMES[raw_operation],
        message=f"{phase_label}：{_OPERATION_LABELS[raw_operation]} {percent}%",
        percent=percent,
        current=int(match.group("current").replace(",", "")),
        total=int(match.group("total").replace(",", "")),
        detail=detail[:200] or None,
    )


def _pump_stream(
    stream_name: str,
    stream: BinaryIO,
    output_queue: queue.Queue[tuple[str, bytes | None]],
) -> None:
    try:
        read = getattr(stream, "read1", stream.read)
        while True:
            chunk = read(8192)
            if not chunk:
                break
            output_queue.put((stream_name, chunk))
    finally:
        output_queue.put((stream_name, None))


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
