import subprocess
from pathlib import Path
from uuid import uuid4

from app.models.review import PullRequestInfo


class GitToolError(RuntimeError):
    pass


class GitTool:
    def __init__(self, workdir: Path | None = None, git_executable: str = "git") -> None:
        from app.core.config import settings
        self._workdir = workdir or settings.repoguardian_workdir
        self._git = git_executable

    def get_file_content(
        self, repo_path: str | Path, file_path: str, start_line: int = 1, end_line: int | None = None
    ) -> str:
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
        self._workdir.mkdir(parents=True, exist_ok=True)
        repo_dir = self._workdir / f"{pr.owner}-{pr.repo}-{pr.number}-{uuid4().hex[:8]}"

        self._run([self._git, "clone", "--no-checkout", pr.clone_url, str(repo_dir)])
        self._fetch_ref(repo_dir, "origin", pr.base.sha, pr.base.ref)
        self._fetch_ref(repo_dir, pr.head.repo_clone_url, pr.head.sha, pr.head.ref)
        diff = self._run(
            [self._git, "-C", str(repo_dir), "diff", "--unified=80", pr.base.sha, "FETCH_HEAD"]
        )
        self._run([self._git, "-C", str(repo_dir), "checkout", "--detach", "FETCH_HEAD"])
        return repo_dir, diff

    @staticmethod
    def _run(command: list[str]) -> str:
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
