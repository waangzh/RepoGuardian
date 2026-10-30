"""Bounded, immutable GitHub blob reads for the human diff viewer."""

import asyncio
import base64
import binascii
import hashlib
import json
import re
from urllib.parse import quote, urlsplit

import httpx

from app.review.domain import Run
from app.core.paths import validate_repo_relative_path as _validate_repo_relative_path
from app.models.file_view import FileContextResponse
from app.review.tool_scope import is_sensitive_repository_change

MAX_FILE_BYTES = 512 * 1024
MAX_TREE_BYTES = 8 * 1024 * 1024


class FileContextError(ValueError):
    """Safe, user-facing failure without upstream bodies or credentials."""


async def _get(client: httpx.AsyncClient, path: str, limit: int) -> dict:
    async with client.stream("GET", f"https://api.github.com{path}") as response:
        if response.status_code != 200:
            raise FileContextError("固定快照源码不可用，请检查 GitHub 访问权限或稍后重试")
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > limit:
                raise FileContextError("源码或仓库目录超过预览大小限制")
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise FileContextError("GitHub 返回了无效的快照内容") from exc
    if not isinstance(value, dict):
        raise FileContextError("GitHub 返回了无效的快照内容")
    return value


def _repository(url: str) -> str:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.query or parsed.fragment
            or not re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", parsed.path)):
        raise FileContextError("任务仓库地址不支持固定快照读取")
    parts = parsed.path.removesuffix(".git").strip("/").split("/")
    if any(part in {".", ".."} for part in parts):
        raise FileContextError("任务仓库地址无效")
    return "/".join(quote(part, safe="") for part in parts)


async def _lines(client: httpx.AsyncClient, repo: str, sha: str, path: str) -> list[str]:
    if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        raise FileContextError("任务缺少有效的固定提交版本")
    commit = await _get(client, f"/repos/{repo}/git/commits/{sha}", 256 * 1024)
    tree_sha = commit.get("tree", {}).get("sha") if isinstance(commit.get("tree"), dict) else None
    if commit.get("sha") != sha or not isinstance(tree_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", tree_sha):
        raise FileContextError("提交与目录快照不一致")
    tree = await _get(client, f"/repos/{repo}/git/trees/{tree_sha}?recursive=1", MAX_TREE_BYTES)
    if tree.get("truncated") is not False or not isinstance(tree.get("tree"), list):
        raise FileContextError("仓库目录不完整，不能安全读取上下文")
    entry = next((item for item in tree["tree"] if isinstance(item, dict) and item.get("path") == path), None)
    if not entry or entry.get("type") != "blob" or entry.get("mode") not in {"100644", "100755"}:
        raise FileContextError("固定快照中没有可读取的普通文件")
    blob_sha = entry.get("sha", "")
    if not isinstance(blob_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", blob_sha):
        raise FileContextError("文件对象标识无效")
    size = entry.get("size")
    if not isinstance(size, int) or not 0 <= size <= MAX_FILE_BYTES:
        raise FileContextError("文件超过 512 KiB 上下文预览限制")
    blob = await _get(client, f"/repos/{repo}/git/blobs/{blob_sha}", MAX_FILE_BYTES * 2)
    try:
        if blob.get("encoding") != "base64" or not isinstance(blob.get("content"), str):
            raise ValueError("invalid blob")
        content = base64.b64decode("".join(blob["content"].split()), validate=True)
        digest = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
        if len(content) != size or len(content) > MAX_FILE_BYTES or digest != blob_sha.lower() or b"\x00" in content:
            raise ValueError("invalid blob")
        text = content.decode("utf-8")
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise FileContextError("文件不是有效的 UTF-8 文本，或与固定快照不一致") from exc
    # Git hunks split on LF and retain CR; do not normalize CRLF or Unicode separators.
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    if len(lines) > 50_000:
        raise FileContextError("文件超过 50000 行上下文预览限制")
    return lines


async def load_file_context(task: Run, file_id: int, client: httpx.AsyncClient) -> FileContextResponse:
    if file_id < 0 or file_id >= len(task.changed_files):
        raise FileContextError("变更文件不存在")
    if task.snapshot is None or task.pr is None:
        raise FileContextError("历史任务缺少固定快照，无法展开额外上下文")
    if any(not re.fullmatch(r"[0-9a-fA-F]{40}", sha)
           for sha in (task.snapshot.head_sha, task.snapshot.base_sha)):
        raise FileContextError("任务缺少有效的固定提交版本")
    file = task.changed_files[file_id]
    if file.is_binary or is_sensitive_repository_change(file.file_path, file.old_file_path):
        raise FileContextError("此文件不允许读取额外上下文")
    try:
        _validate_repo_relative_path(file.file_path)
        _validate_repo_relative_path(file.old_file_path or file.file_path)
    except ValueError as exc:
        raise FileContextError("任务文件路径无效") from exc
    async with asyncio.timeout(45):
        base = [] if file.change_type == "added" else await _lines(
            client, _repository(task.pr.base.repo_clone_url), task.snapshot.base_sha,
            file.old_file_path or file.file_path)
        head = [] if file.change_type == "deleted" else await _lines(
            client, _repository(task.pr.head.repo_clone_url), task.snapshot.head_sha, file.file_path)
    return FileContextResponse(file_id=file_id, file_path=file.file_path, snapshot=task.snapshot,
                               base_lines=base, head_lines=head)
