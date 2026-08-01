"""Review Unit 只读文件访问的统一安全策略。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path, PurePosixPath


class ReviewPathPolicyError(ValueError):
    """请求的仓库路径不满足只读审查安全策略。"""


_SENSITIVE_PARTS = frozenset({
    ".git", ".ssh", ".aws", ".azure", ".kube", ".gnupg", ".terraform",
})
_SENSITIVE_NAMES = frozenset({
    ".env", ".netrc", ".npmrc", ".pypirc", "credentials", "credentials.json",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "known_hosts",
})
_PRIVATE_KEY_SUFFIXES = (".pem", ".p12", ".pfx", ".key")


def is_sensitive_repository_path(file_path: str) -> bool:
    """按规范化的仓库相对路径判断是否属于禁止读取的敏感路径。"""
    path = PurePosixPath(file_path)
    lowered_parts = tuple(part.casefold() for part in path.parts)
    name = lowered_parts[-1] if lowered_parts else ""
    return (
        any(part in _SENSITIVE_PARTS for part in lowered_parts)
        or name in _SENSITIVE_NAMES
        or name.startswith(".env.")
        or name.endswith(_PRIVATE_KEY_SUFFIXES)
        or lowered_parts[:2] == (".config", "gcloud")
        or lowered_parts[:2] == (".docker", "config.json")
    )


def list_git_tracked_files(repository_root: str | Path, git_executable: str = "git") -> set[str]:
    """返回 Git 索引中的路径；非 Git fixture 返回空集合，由调用方决定是否降级。"""
    root = Path(repository_root).resolve()
    completed = subprocess.run(
        [git_executable, "-C", str(root), "ls-files", "-z", "--cached"],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        return set()
    return {
        item.decode("utf-8", errors="surrogateescape")
        for item in completed.stdout.split(b"\0")
        if item
    }


def validate_repository_file(
    repository_root: str | Path,
    file_path: str,
    *,
    tracked_files: set[str] | None = None,
    require_tracked_if_git: bool = True,
) -> Path:
    """执行敏感路径、Git tracked 与 realpath 根目录三重校验。"""
    if not isinstance(file_path, str) or not file_path or "\\" in file_path or "\x00" in file_path:
        raise ReviewPathPolicyError("repository path must be a normalized POSIX relative path")
    relative = PurePosixPath(file_path)
    if (
        relative.is_absolute()
        or relative.as_posix() != file_path
        or any(part in {"", ".", ".."} for part in relative.parts)
        or ":" in file_path
    ):
        raise ReviewPathPolicyError("repository path traversal is not allowed")
    if is_sensitive_repository_path(file_path):
        raise ReviewPathPolicyError(f"sensitive repository path is not readable: {file_path}")

    root = Path(repository_root).resolve(strict=True)
    candidate = root.joinpath(*relative.parts).resolve(strict=True)
    try:
        if os.path.commonpath((str(root), str(candidate))) != str(root):
            raise ReviewPathPolicyError(f"resolved path escapes repository root: {file_path}")
    except ValueError as exc:
        raise ReviewPathPolicyError(f"resolved path escapes repository root: {file_path}") from exc
    if not candidate.is_file():
        raise ReviewPathPolicyError(f"repository path is not a regular file: {file_path}")

    git_dir_exists = (root / ".git").exists()
    effective_tracked = tracked_files
    if effective_tracked is None and git_dir_exists:
        effective_tracked = list_git_tracked_files(root)
    if require_tracked_if_git and git_dir_exists and file_path not in (effective_tracked or set()):
        raise ReviewPathPolicyError(f"repository path is not Git tracked: {file_path}")
    return candidate
