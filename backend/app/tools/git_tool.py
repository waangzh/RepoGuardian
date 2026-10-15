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
    git_safety_environment,
    list_git_tracked_files,
    validate_repository_file,
)
from app.tools.workspace_cleanup import cleanup_workspace

logger = logging.getLogger("RepoGuardian.GitTool")

# 生产、Preview、CLI 与评测共用的 Diff 策略：内容基准取唯一 merge base，
# PR 声明 base 只保留身份用途。修改命令或上下文行数时必须同步该版本号。
DIFF_POLICY = "merge_base"
DIFF_POLICY_VERSION = "merge-base-v1"
DIFF_CONTEXT_LINES = 80

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


class DiffPreparationError(GitToolError):
    """无法确定唯一的内容基准，拒绝退化为两点比较。"""

    def __init__(self, reason: str, bases: list[str] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.bases = bases or []


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

    def get_tree_entries(self, repo_path: str | Path, revision: str, paths: list[str]) -> dict:
        """只读固定 tree：literal pathspec、NUL 分隔、完整 object ID，不访问工作树文件。"""
        from app.models.review import FileChangeSide
        from app.review.tool_scope import is_sensitive_repository_change

        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", revision):
            raise ValueError("tree evidence requires an immutable revision")
        repo_dir = self._validate_worktree(repo_path)
        selected = sorted({FileChangeSide(path=path).path for path in paths})
        if any(is_sensitive_repository_change(path) for path in selected):
            raise ValueError("sensitive tree evidence path")
        entries = {}
        for offset in range(0, len(selected), 32):
            batch = selected[offset:offset + 32]
            raw = self._run([self._git, "--literal-pathspecs", "-C", str(repo_dir), "ls-tree",
                "--full-tree", "-l", "-z", revision, "--", *batch])
            for entry in raw.split("\0"):
                if not entry:
                    continue
                fields, path = entry.split("\t", 1)
                mode, kind, identity, size = fields.split()
                if path in batch and kind in {"blob", "commit"}:
                    entries[path] = {"blob_id": identity, "mode": mode,
                        "size": int(size) if size != "-" else None}
        return entries

    def tracking_evidence(self, repo_path: str | Path, revision: str, queries: list[str]) -> dict:
        """查询不可变 Head tree 的精确文件名；不访问正文、索引或工作树。"""
        from app.models.review import CrossUnitFollowupRequest
        from app.review.tool_scope import is_sensitive_repository_path

        queries = CrossUnitFollowupRequest.validate_tracking_queries(queries)
        if not queries or len(queries) > 12 or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", revision):
            raise ValueError("tracking evidence requires bounded queries and an immutable revision")
        root = self._validate_worktree(repo_path)
        raw = self._run([self._git, "-C", str(root), "ls-tree", "-r", "--name-only", "-z", revision])
        matches = sorted(path for path in raw.split("\0") if path and path.rsplit("/", 1)[-1] in queries)
        visible, size = [], 0
        for path in matches:
            if not is_sensitive_repository_path(path) and size + len(path) <= 3000 and len(visible) < 40:
                visible.append(path)
                size += len(path)
        return {"head_sha": revision, "queries": queries, "match_mode": "exact_basename_all_directories",
            "matches": visible, "matched_count": len(matches), "complete": len(visible) == len(matches),
            "scope": "committed_head_tree_only"}

    def get_file_content_at_revision(
        self, repo_path: str | Path, revision: str, file_path: str
    ) -> str | None:
        """读取服务端选定 revision 的文件，不切换工作树。"""
        repo_dir = self._validate_worktree(repo_path)
        completed = subprocess.run(
            [self._git, "-C", str(repo_dir), "show", f"{revision}:{file_path}"],
            check=False,
            capture_output=True,
            env=git_safety_environment(),
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
            head_commit = self._run(
                [self._git, "-C", str(repo_dir), "rev-parse", "FETCH_HEAD"],
                cancel_event=cancel_event,
            ).strip()
            if head_commit != pr.head.sha:
                raise DiffPreparationError("fetched_head_differs_from_declared")
            effective_base_sha, diff = self.unified_diff(
                repo_dir, pr.base.sha, head_commit, cancel_event=cancel_event
            )
            self._notify(progress_callback, "diff", "PR Diff 已生成", "completed", 100)
            # 检出 head 到工作树（后续静态分析/测试需要）
            self._notify(progress_callback, "checkout", "正在检出 Head 工作树")
            self._run(
                [self._git, "-C", str(repo_dir), "checkout", "--progress", "--detach", head_commit],
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

    def unified_diff(
        self,
        repo_path: str | Path,
        base_sha: str,
        head_sha: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> tuple[str, str]:
        """生产、Preview、CLI 与评测唯一的 Diff 生成函数。

        内容基准是 ``base_sha`` 与 ``head_sha`` 的唯一 merge base；声明 base
        只保留 PR 身份用途。无法确定唯一基准时抛出 ``DiffPreparationError``，
        不回退到两点比较——两点比较会把 base 分支独有提交当作本 PR 的反向
        变更交给 Agent 审查。
        """
        repo_dir = self._validate_worktree(repo_path)
        bases = self.merge_bases(repo_dir, base_sha, head_sha, cancel_event=cancel_event)
        if not bases:
            raise DiffPreparationError("no_merge_base")
        if len(bases) > 1:
            raise DiffPreparationError("multiple_merge_bases", bases)
        effective_base_sha = bases[0]
        diff = self._run(
            [self._git, "-C", str(repo_dir), "diff", "--no-ext-diff",
             f"--unified={DIFF_CONTEXT_LINES}", effective_base_sha, head_sha],
            cancel_event=cancel_event,
        )
        return effective_base_sha, diff

    def merge_bases(
        self,
        repo_path: str | Path,
        base_sha: str,
        head_sha: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> list[str]:
        """列出全部 merge base 候选；无共同祖先返回空列表，不吞掉命令错误。"""
        repo_dir = self._validate_worktree(repo_path)
        if cancel_event is not None and cancel_event.is_set():
            raise GitToolCancelled("Git 操作已取消")
        timeout = self._command_timeout_seconds if self._command_timeout_seconds > 0 else None
        try:
            completed = subprocess.run(
                [self._git, "-C", str(repo_dir), "merge-base", "--all", base_sha, head_sha],
                check=False,
                capture_output=True,
                timeout=timeout,
                env=git_safety_environment(),
            )
        except subprocess.TimeoutExpired as exc:
            raise GitToolTimeout(f"Git merge-base 超过 {timeout:g} 秒超时") from exc
        if cancel_event is not None and cancel_event.is_set():
            raise GitToolCancelled("Git 操作已取消")
        if completed.returncode != 0:
            # 无共同祖先时 git 以退出码 1 返回空输出；其余失败仍是命令错误。
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            if completed.returncode != 1 or detail:
                raise GitToolError(
                    f"Git merge-base 执行失败（exit {completed.returncode}）\n{detail}"
                )
            return []
        return [
            line.strip()
            for line in completed.stdout.decode("utf-8", errors="replace").splitlines()
            if line.strip()
        ]

    def merge_base(self, repo_path: str | Path, base_sha: str, head_sha: str) -> str | None:
        """返回唯一 merge base；不存在或存在多个候选时返回 ``None``。"""
        bases = self.merge_bases(repo_path, base_sha, head_sha)
        return bases[0] if len(bases) == 1 else None

    def review_diff_context(
        self, repo_path: str | Path, base_sha: str | None = None, head_sha: str | None = None
    ) -> dict[str, str]:
        """返回本次审查的内容基准；每次重新计算，避免共享实例跨任务串用。

        缺少任一 SHA、无法确定唯一 merge base、或内容基准等于声明 base 时返回
        空字典，调用方按 ``declared_base`` 处理；空字典不是成功信号。
        """
        if not base_sha or not head_sha:
            return {}
        try:
            effective = self.merge_base(repo_path, base_sha, head_sha)
        except GitToolError:
            logger.warning("无法确定唯一内容基准，保留声明 base 身份", exc_info=True)
            return {}
        if effective is None or effective == base_sha:
            return {}
        return {
            "effective_base_sha": effective,
            "diff_policy": DIFF_POLICY,
            "diff_policy_version": DIFF_POLICY_VERSION,
        }

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
                env=git_safety_environment(),
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
