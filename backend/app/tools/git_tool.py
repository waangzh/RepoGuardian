"""Git 操作工具 —— 克隆仓库、生成 diff、读取文件内容。"""

import subprocess
from pathlib import Path
from uuid import uuid4

from app.models.review import PullRequestInfo


class GitToolError(RuntimeError):
    """Git 命令执行失败时抛出。"""
    pass


class GitTool:
    """在临时目录中执行 Git 操作：浅克隆、fetch refs、生成 unified diff。

    克隆到 settings.repoguardian_workdir 下随机命名的子目录中，
    避免并发任务冲突。
    """

    def __init__(self, workdir: Path | None = None, git_executable: str = "git") -> None:
        from app.core.config import settings
        self._workdir = workdir or settings.repoguardian_workdir
        self._git = git_executable

    def get_file_content(
        self, repo_path: str | Path, file_path: str, start_line: int = 1, end_line: int | None = None
    ) -> str:
        """从检出的仓库中直接读取指定文件的指定行范围（非 git 命令，直接文件 I/O）。"""
        full_path = Path(repo_path) / file_path
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except (OSError, UnicodeDecodeError):
            return ""
        if end_line is None:
            end_line = len(lines)
        start_idx = max(start_line - 1, 0)
        end_idx = min(end_line, len(lines))
        return "".join(lines[start_idx:end_idx])

    def clone_and_diff(self, pr: PullRequestInfo) -> tuple[Path, str]:
        """核心操作：clone → fetch base/head refs → 生成 unified diff → checkout head。

        返回 (仓库临时路径, diff 文本)。
        """
        self._workdir.mkdir(parents=True, exist_ok=True)
        repo_dir = self._workdir / f"{pr.owner}-{pr.repo}-{pr.number}-{uuid4().hex[:8]}"

        # 浅克隆（不带 checkout，节省时间）
        self._run([self._git, "clone", "--no-checkout", pr.clone_url, str(repo_dir)])
        # 分别 fetch base 和 head 的 SHA
        self._fetch_ref(repo_dir, "origin", pr.base.sha, pr.base.ref)
        self._fetch_ref(repo_dir, pr.head.repo_clone_url, pr.head.sha, pr.head.ref)
        # 生成 unified diff（上下文 80 行，足够 LLM 理解）
        diff = self._run(
            [self._git, "-C", str(repo_dir), "diff", "--unified=80", pr.base.sha, "FETCH_HEAD"]
        )
        # 检出 head 到工作树（后续静态分析/测试需要）
        self._run([self._git, "-C", str(repo_dir), "checkout", "--detach", "FETCH_HEAD"])
        return repo_dir, diff

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

    @staticmethod
    def _run(command: list[str]) -> str:
        """同步执行 git 命令，失败时抛出 GitToolError。"""
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise GitToolError(f"Command failed: {' '.join(command)}\n{detail}")
        return completed.stdout

    def _fetch_ref(self, repo_dir: Path, remote: str, sha: str, ref: str) -> None:
        """拉取指定 SHA，失败时回退到 ref 名称重试。"""
        command = [self._git, "-C", str(repo_dir), "fetch", remote, sha]
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode == 0:
            return

        # 回退：按 ref 名称 fetch（某些 fork PR 的 SHA 不在 origin 中）
        fallback = [self._git, "-C", str(repo_dir), "fetch", remote, ref]
        fallback_completed = subprocess.run(
            fallback,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if fallback_completed.returncode != 0:
            detail = (fallback_completed.stderr or completed.stderr or fallback_completed.stdout).strip()
            raise GitToolError(
                "Command failed: "
                f"{' '.join(command)}; fallback {' '.join(fallback)} also failed\n{detail}"
            )
