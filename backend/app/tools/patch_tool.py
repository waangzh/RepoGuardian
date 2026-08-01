"""Patch 工具 —— 在临时仓库中通过 git apply 安全应用 unified diff。

安全边界：
    - 只允许在 settings.repoguardian_workdir 下的临时目录中操作
    - 先 git apply --check 验证，通过后才正式 apply
    - 不 commit、不 push、不写回用户真实仓库
"""

import asyncio
import hashlib
import subprocess
from io import StringIO
from pathlib import Path
from typing import Any

from unidiff import PatchSet

from app.models.review import (
    PatchApplyCheck,
    PatchApplyCheckStatus,
    PatchEligibilityDecision,
    PatchProposal,
    PatchResult,
    PatchStatus,
    _validate_repo_relative_path,
)
from app.services.patch_presentation import build_patch_presentation
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
            patch.status = PatchStatus.abandoned
            patch.error = "Patch diff is empty"
            return patch
        if not (repo / ".git").exists():
            patch.status = PatchStatus.abandoned
            patch.error = "Repository path is not a git worktree"
            return patch

        # 1) 试运行
        try:
            check = await _run_git_apply(repo, patch.diff_content, check_only=True)
        except subprocess.TimeoutExpired:
            patch.status = PatchStatus.abandoned
            patch.error = "git apply --check timed out"
            return patch
        if check.returncode != 0:
            patch.status = PatchStatus.abandoned
            patch.error = (check.stderr or check.stdout or "git apply --check failed")[-8000:]
            return patch

        # 2) 正式应用
        try:
            applied = await _run_git_apply(repo, patch.diff_content, check_only=False)
        except subprocess.TimeoutExpired:
            patch.status = PatchStatus.abandoned
            patch.error = "git apply timed out"
            return patch
        if applied.returncode != 0:
            patch.status = PatchStatus.abandoned
            patch.error = (applied.stderr or applied.stdout or "git apply failed")[-8000:]
            return patch

        patch.status = PatchStatus.validation_pending
        patch.error = None
        return patch

    async def check_candidate(
        self,
        repo_path: str | Path,
        patch: PatchProposal,
        decision: PatchEligibilityDecision,
        current_head_sha: str,
    ) -> PatchProposal:
        """在临时工作树中检查并短暂应用候选补丁；不执行目标代码。"""
        repo = ensure_repo_path(repo_path)
        failure = _validate_candidate_metadata(patch, decision, current_head_sha)
        if failure:
            return _failed_candidate(patch, failure, current_head_sha)

        try:
            proposed_signature = _diff_signature(patch.unified_diff)
            check = await _run_git_apply(
                repo, patch.unified_diff, check_only=True
            )
            if check.returncode != 0:
                detail = (check.stderr or check.stdout or "git apply --check failed")[-8000:]
                return _failed_candidate(patch, detail, current_head_sha)

            applied = await _run_git_apply(
                repo, patch.unified_diff, check_only=False
            )
            if applied.returncode != 0:
                detail = (applied.stderr or applied.stdout or "git apply failed")[-8000:]
                return _failed_candidate(patch, detail, current_head_sha)

            staged = await _run_git_stage(repo)
            if staged.returncode != 0:
                detail = (staged.stderr or staged.stdout or "git add failed")[-8000:]
                return _failed_candidate(patch, detail, current_head_sha)
            actual = await _run_git_diff(repo)
            if actual.returncode != 0:
                detail = (actual.stderr or actual.stdout or "git diff failed")[-8000:]
                return _failed_candidate(patch, detail, current_head_sha)
            if _diff_signature(actual.stdout) != proposed_signature:
                return _failed_candidate(
                    patch,
                    "应用后的 git diff 与提议 patch 不一致",
                    current_head_sha,
                )
        except (ValueError, subprocess.TimeoutExpired) as exc:
            return _failed_candidate(
                patch,
                f"候选补丁检查失败: {type(exc).__name__}: {exc}",
                current_head_sha,
            )

        patch.status = PatchStatus.unverified
        patch.patch_sha = normalized_patch_sha(patch.unified_diff)
        patch.apply_check = PatchApplyCheck(
            status=PatchApplyCheckStatus.passed,
            detail="git apply --check 与应用后 diff 一致；这不代表补丁功能正确。",
            checked_head_sha=current_head_sha,
            worktree_clean=None,
        )
        patch.presentation = build_patch_presentation(patch)
        patch.error = None
        return patch


async def _run_git_apply(
    repo: Path,
    diff_content: str,
    check_only: bool,
    update_index: bool = False,
) -> subprocess.CompletedProcess[str]:
    """通过子进程执行 git apply，通过 stdin 传入 patch 内容。"""
    # 上下文必须逐字符匹配，避免将补丁应用到仅空白不同的相似代码块。
    # 仅放宽工作树 CRLF 等空白差异；应用后仍会与提议 diff 做逐行签名比对，
    # 因此缩进或内容不一致不会被误认为同一补丁。
    command = ["git", "apply", "--ignore-space-change"]
    if check_only:
        command.append("--check")
    if update_index:
        command.append("--index")
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


async def _run_git_diff(repo: Path) -> subprocess.CompletedProcess[str]:
    return await asyncio.to_thread(
        subprocess.run,
        ["git", "diff", "--cached", "--binary", "--no-ext-diff", "HEAD", "--"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


async def _run_git_stage(repo: Path) -> subprocess.CompletedProcess[str]:
    """固定参数暂存临时 clone 的补丁结果，以便新文件也进入实际 diff。"""
    return await asyncio.to_thread(
        subprocess.run,
        ["git", "add", "-A", "--"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


def _validate_candidate_metadata(
    patch: PatchProposal,
    decision: PatchEligibilityDecision,
    current_head_sha: str,
) -> str | None:
    if patch.head_sha != current_head_sha:
        patch.stale = True
        return "Patch 基于过期 Head SHA，必须重新生成"
    if set(patch.issue_ids) != {decision.issue_id}:
        return "Patch issue_ids 与资格决策不一致"
    if "\x00" in patch.unified_diff:
        return "unified diff 包含 NUL"
    if "GIT binary patch" in patch.unified_diff or "Binary files " in patch.unified_diff:
        return "不允许新增或修改二进制文件"
    try:
        parsed = PatchSet(StringIO(patch.unified_diff))
    except Exception as exc:
        return f"unified diff 无法解析: {exc}"
    if not parsed:
        return "unified diff 为空"

    actual_files: list[str] = []
    changed_lines = 0
    for patched_file in parsed:
        if bool(getattr(patched_file, "is_binary_file", False)):
            return "不允许新增或修改二进制文件"
        source = _safe_diff_path(patched_file.source_file, "a/")
        target = _safe_diff_path(patched_file.target_file, "b/")
        if source is None and patched_file.source_file != "/dev/null":
            return f"非法 source 路径: {patched_file.source_file}"
        if target is None and patched_file.target_file != "/dev/null":
            return f"非法 target 路径: {patched_file.target_file}"
        for path in (source, target):
            if path and path not in actual_files:
                actual_files.append(path)
            if path and _is_prohibited_path(path):
                return f"禁止修改路径: {path}"
        changed_lines += int(patched_file.added) + int(patched_file.removed)

    if set(patch.touched_files) != set(actual_files):
        return "touched_files 与 unified diff 的实际文件不一致"
    if not set(actual_files) <= set(decision.allowed_files):
        return "Patch 修改了 allowed_files 之外的文件"
    if len(actual_files) > decision.max_files:
        return f"Patch 文件数超过上限 {decision.max_files}"
    if changed_lines == 0 or changed_lines > decision.max_changed_lines:
        return f"Patch 变更行数超过上限 {decision.max_changed_lines}"
    return None


def extract_touched_files(diff_content: str) -> list[str]:
    """从可解析 unified diff 提取实际 source/target 仓库路径。"""
    parsed = PatchSet(StringIO(diff_content))
    paths: list[str] = []
    for patched_file in parsed:
        source = _safe_diff_path(patched_file.source_file, "a/")
        target = _safe_diff_path(patched_file.target_file, "b/")
        for path in (source, target):
            if path and path not in paths:
                paths.append(path)
    return paths


def _safe_diff_path(value: str, expected_prefix: str) -> str | None:
    if value == "/dev/null":
        return None
    if not value.startswith(expected_prefix):
        return None
    path = value[len(expected_prefix):]
    try:
        return _validate_repo_relative_path(path)
    except ValueError:
        return None


def _is_prohibited_path(path: str) -> bool:
    lowered = path.lower()
    return (
        lowered == ".env"
        or lowered.startswith(".env.")
        or lowered.startswith(".git/")
        or lowered.startswith(".repoguardian/")
    )


def _failed_candidate(
    patch: PatchProposal,
    detail: str,
    current_head_sha: str,
) -> PatchProposal:
    patch.status = PatchStatus.abandoned
    patch.error = detail
    patch.apply_check = PatchApplyCheck(
        status=PatchApplyCheckStatus.failed,
        detail=detail,
        checked_head_sha=current_head_sha,
        worktree_clean=None,
    )
    patch.presentation = build_patch_presentation(patch)
    return patch


def _diff_signature(diff_content: str) -> tuple[Any, ...]:
    try:
        parsed = PatchSet(StringIO(diff_content))
    except Exception as exc:
        raise ValueError(f"unified diff cannot be parsed: {exc}") from exc
    signature: list[Any] = []
    for patched_file in parsed:
        changes: list[tuple[str, str]] = []
        for hunk in patched_file:
            changes.extend(
                (
                    "+" if line.is_added else "-",
                    line.value.rstrip("\r\n"),
                )
                for line in hunk
                if line.is_added or line.is_removed
            )
        signature.append((
            patched_file.source_file,
            patched_file.target_file,
            bool(patched_file.is_added_file),
            bool(patched_file.is_removed_file),
            tuple(changes),
        ))
    return tuple(signature)


def _normalize_diff(diff_content: str) -> str:
    return diff_content.replace("\r\n", "\n").rstrip("\n") + "\n"


def normalized_patch_sha(diff_content: str) -> str:
    """返回 RepoGuardian 跨平台统一使用的 patch SHA-256。"""
    return hashlib.sha256(_normalize_diff(diff_content).encode("utf-8")).hexdigest()
