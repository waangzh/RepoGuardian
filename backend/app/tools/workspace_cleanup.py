"""任务临时工作目录的安全清理与孤儿目录回收。"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

logger = logging.getLogger("RepoGuardian.WorkspaceCleanup")


@dataclass(frozen=True)
class WorkspaceReapResult:
    scanned: int = 0
    removed: int = 0
    failed: int = 0
    skipped_recent: int = 0


def _remove_readonly(
    function: Callable[[str], None], path: str, error: BaseException
) -> None:
    """清除 Windows Git object 的只读属性后重试原删除操作。"""
    if not isinstance(error, PermissionError):
        raise error
    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    function(path)


def _resolve_cleanup_target(repo_path: str | Path, workdir: str | Path) -> tuple[Path, Path]:
    root = Path(workdir).resolve()
    raw_target = Path(repo_path)
    if raw_target.is_symlink():
        raise ValueError(f"refusing to remove symlink workspace: {raw_target}")
    target = raw_target.resolve()
    if target == root or not target.is_relative_to(root):
        raise ValueError(f"workspace path is outside configured workdir: {target}")
    return root, target


def cleanup_workspace(
    repo_path: str | Path,
    *,
    workdir: str | Path | None = None,
    attempts: int = 3,
    retry_delay_seconds: float = 0.1,
) -> bool:
    """安全删除一个任务工作目录；失败时保留目录并记录完整异常。"""
    if workdir is None:
        from app.core.config import settings

        workdir = settings.repoguardian_workdir
    try:
        _, target = _resolve_cleanup_target(repo_path, workdir)
    except (OSError, ValueError) as exc:
        logger.warning("拒绝清理临时工作目录 %s: %s", repo_path, exc)
        return False

    if not target.exists():
        return True

    attempts = max(1, attempts)
    for attempt in range(1, attempts + 1):
        try:
            shutil.rmtree(target, onexc=_remove_readonly)
            logger.info("已清理临时工作目录: %s", target)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if attempt == attempts:
                logger.warning(
                    "清理临时工作目录失败（已重试 %d 次）: %s",
                    attempts,
                    target,
                    exc_info=True,
                )
                return False
            time.sleep(retry_delay_seconds * attempt)
    return False


def reap_orphaned_workspaces(
    *,
    workdir: str | Path,
    older_than_seconds: float,
    active_paths: Iterable[str | Path] = (),
    now: float | None = None,
) -> WorkspaceReapResult:
    """回收 workdir 下超过 TTL 的直接子目录，并跳过当前活跃目录。"""
    root = Path(workdir).resolve()
    if not root.exists():
        return WorkspaceReapResult()

    active = {Path(path).resolve() for path in active_paths}
    current_time = time.time() if now is None else now
    scanned = removed = failed = skipped_recent = 0
    for child in root.iterdir():
        if not child.is_dir() or child.is_symlink():
            continue
        scanned += 1
        resolved = child.resolve()
        if resolved in active:
            continue
        try:
            age_seconds = max(0.0, current_time - child.stat().st_mtime)
        except OSError:
            logger.warning("无法读取临时工作目录状态: %s", child, exc_info=True)
            failed += 1
            continue
        if age_seconds < older_than_seconds:
            skipped_recent += 1
            continue
        if cleanup_workspace(child, workdir=root):
            removed += 1
        else:
            failed += 1
    return WorkspaceReapResult(
        scanned=scanned,
        removed=removed,
        failed=failed,
        skipped_recent=skipped_recent,
    )
