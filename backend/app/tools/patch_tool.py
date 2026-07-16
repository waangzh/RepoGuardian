"""Patch 工具 —— 在临时仓库中通过 git apply 安全应用 unified diff。

安全边界：
    - 只允许在 settings.repoguardian_workdir 下的临时目录中操作
    - 先 git apply --check 验证，通过后才正式 apply
    - 不 commit、不 push、不写回用户真实仓库
"""

import asyncio
import subprocess
from pathlib import Path
from typing import Any

from app.models.review import PatchResult, PatchStatus
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
        """在临时仓库中执行 git apply，先 --check 验证再正式应用。"""
        repo = ensure_repo_path(repo_path)

        # 前置校验
        if not patch.diff_content.strip():
            patch.status = PatchStatus.apply_failed
            patch.error = "Patch diff is empty"
            return patch
        if not (repo / ".git").exists():
            patch.status = PatchStatus.apply_failed
            patch.error = "Repository path is not a git worktree"
            return patch

        # 1) 试运行
        try:
            check = await _run_git_apply(repo, patch.diff_content, check_only=True)
        except subprocess.TimeoutExpired:
            patch.status = PatchStatus.apply_failed
            patch.error = "git apply --check timed out"
            return patch
        if check.returncode != 0:
            patch.status = PatchStatus.apply_failed
            patch.error = (check.stderr or check.stdout or "git apply --check failed")[-8000:]
            return patch

        # 2) 正式应用
        try:
            applied = await _run_git_apply(repo, patch.diff_content, check_only=False)
        except subprocess.TimeoutExpired:
            patch.status = PatchStatus.apply_failed
            patch.error = "git apply timed out"
            return patch
        if applied.returncode != 0:
            patch.status = PatchStatus.apply_failed
            patch.error = (applied.stderr or applied.stdout or "git apply failed")[-8000:]
            return patch

        patch.status = PatchStatus.applied
        patch.error = None
        return patch


async def _run_git_apply(repo: Path, diff_content: str, check_only: bool) -> subprocess.CompletedProcess[str]:
    """通过子进程执行 git apply，通过 stdin 传入 patch 内容。"""
    # 仓库检出可能采用 CRLF；忽略上下文空白差异但仍严格校验路径和内容。
    command = ["git", "apply", "--ignore-space-change"]
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
        timeout=30,
    )
