"""补丁身份计算；候选补丁生成与 git apply 已下线，仅保留跨平台 SHA。"""

import hashlib


def _normalize_diff(diff_content: str) -> str:
    return diff_content.replace("\r\n", "\n").rstrip("\n") + "\n"


def normalized_patch_sha(diff_content: str) -> str:
    """返回 RepoGuardian 跨平台统一使用的 patch SHA-256。"""
    return hashlib.sha256(_normalize_diff(diff_content).encode("utf-8")).hexdigest()
