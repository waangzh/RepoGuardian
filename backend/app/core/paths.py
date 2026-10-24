"""仓库相对路径的规范校验；模型与计划共用同一规则。"""

from pathlib import PurePosixPath


def validate_repo_relative_path(value: str) -> str:
    """只接受与仓库索引格式一致的 POSIX 相对路径。"""
    if not isinstance(value, str) or not value or len(value) > 260:
        raise ValueError("repository path must be a non-empty relative path")
    if "\\" in value or "\x00" in value or value.startswith(("/", "~")) or ":" in value:
        raise ValueError("repository path must use a safe POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("repository path traversal is not allowed")
    if path.as_posix() != value:
        raise ValueError("repository path must be normalized")
    return value
