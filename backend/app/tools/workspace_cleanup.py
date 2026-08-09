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
    skipped_active: int = 0
    skipped_recent: int = 0
    reclaimed_bytes: int = 0


@dataclass(frozen=True)
class WorkspaceScanResult:
    scanned: int = 0
    eligible: int = 0
    eligible_bytes: int = 0
    failed: int = 0
    skipped_active: int = 0
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


def inspect_orphaned_workspaces(
    *,
    workdir: str | Path,
    older_than_seconds: float,
    active_paths: Iterable[str | Path] = (),
    now: float | None = None,
) -> WorkspaceScanResult:
    """只读扫描超过 TTL 的非活动工作目录，并估算可回收空间。"""
    _, result = _classify_workspaces(
        workdir=workdir,
        older_than_seconds=older_than_seconds,
        active_paths=active_paths,
        now=now,
    )
    return result


def reap_orphaned_workspaces(
    *,
    workdir: str | Path,
    older_than_seconds: float,
    active_paths: Iterable[str | Path] = (),
    now: float | None = None,
) -> WorkspaceReapResult:
    """回收 workdir 下超过 TTL 的直接子目录，并跳过当前活跃目录。"""
    candidates, scan = _classify_workspaces(
        workdir=workdir,
        older_than_seconds=older_than_seconds,
        active_paths=active_paths,
        now=now,
    )
    root = Path(workdir).resolve()
    removed = reclaimed_bytes = 0
    failed = scan.failed
    for child, size_bytes in candidates:
        if cleanup_workspace(child, workdir=root):
            removed += 1
            reclaimed_bytes += size_bytes
        else:
            failed += 1
    return WorkspaceReapResult(
        scanned=scan.scanned,
        removed=removed,
        failed=failed,
        skipped_active=scan.skipped_active,
        skipped_recent=scan.skipped_recent,
        reclaimed_bytes=reclaimed_bytes,
    )


def _classify_workspaces(
    *,
    workdir: str | Path,
    older_than_seconds: float,
    active_paths: Iterable[str | Path],
    now: float | None,
) -> tuple[list[tuple[Path, int]], WorkspaceScanResult]:
    root = Path(workdir).resolve()
    if not root.exists():
        return [], WorkspaceScanResult()

    active = {Path(path).resolve() for path in active_paths}
    current_time = time.time() if now is None else now
    candidates: list[tuple[Path, int]] = []
    scanned = failed = skipped_active = skipped_recent = 0
    for child in root.iterdir():
        if not child.is_dir() or child.is_symlink():
            continue
        scanned += 1
        resolved = child.resolve()
        if resolved in active:
            skipped_active += 1
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
        candidates.append((child, _directory_size(child)))

    return candidates, WorkspaceScanResult(
        scanned=scanned,
        eligible=len(candidates),
        eligible_bytes=sum(size for _, size in candidates),
        failed=failed,
        skipped_active=skipped_active,
        skipped_recent=skipped_recent,
    )


def _directory_size(root: Path) -> int:
    total = 0
    for directory, _, files in os.walk(root, followlinks=False):
        for name in files:
            try:
                total += (Path(directory) / name).stat().st_size
            except OSError:
                logger.debug("无法统计临时文件大小: %s", Path(directory) / name, exc_info=True)
    return total
