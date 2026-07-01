import asyncio
import subprocess
from pathlib import Path
from typing import Any

from app.models.review import PatchResult
from app.tools.base import BaseTool
from app.tools.command_runner import ensure_repo_path


class PatchTool(BaseTool):
    name = "patch_tool"
    description = "Apply unified diffs inside the temporary repository only."

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        repo_path = ensure_repo_path(kwargs["repo_path"])
        patch = PatchResult.model_validate(kwargs["patch"])
        applied = await self.apply(repo_path, patch)
        return {"patches": [applied.model_dump(mode="json")]}

    async def apply(self, repo_path: str | Path, patch: PatchResult) -> PatchResult:
        repo = ensure_repo_path(repo_path)
        if not patch.diff_content.strip():
            patch.status = "apply_failed"
            patch.error = "Patch diff is empty"
            return patch
        if not (repo / ".git").exists():
            patch.status = "apply_failed"
            patch.error = "Repository path is not a git worktree"
            return patch

        check = await _run_git_apply(repo, patch.diff_content, check_only=True)
        if check.returncode != 0:
            patch.status = "apply_failed"
            patch.error = (check.stderr or check.stdout or "git apply --check failed")[-8000:]
            return patch

        applied = await _run_git_apply(repo, patch.diff_content, check_only=False)
        if applied.returncode != 0:
            patch.status = "apply_failed"
            patch.error = (applied.stderr or applied.stdout or "git apply failed")[-8000:]
            return patch

        patch.status = "applied"
        patch.error = None
        return patch


async def _run_git_apply(repo: Path, diff_content: str, check_only: bool) -> subprocess.CompletedProcess[str]:
    command = ["git", "apply"]
    if check_only:
        command.append("--check")
    command.append("-")
    return await asyncio.to_thread(
        subprocess.run,
        command,
        cwd=repo,
        input=diff_content,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
