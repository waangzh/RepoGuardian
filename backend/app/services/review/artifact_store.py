"""受控本地 artifact 存储；数据库只保存内容寻址引用。"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4
from tempfile import NamedTemporaryFile
from urllib.parse import unquote

from app.core.config import settings

_write_lock = threading.Lock()


@dataclass(frozen=True)
class StoredArtifact:
    id: str
    uri: str
    sha256: str
    size_bytes: int


class LocalArtifactStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or settings.repoguardian_artifact_dir).resolve()

    def put_text(self, *, task_id: str, kind: str, content: str) -> StoredArtifact:
        # Parallel native tools can produce the same evidence simultaneously.
        # Serialize publication of blobs so Windows readers never race a replace.
        with _write_lock:
            return self._put_text(task_id=task_id, kind=kind, content=content)

    def _put_text(self, *, task_id: str, kind: str, content: str) -> StoredArtifact:
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", kind):
            raise ValueError("invalid artifact kind")
        data = content.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        artifact_id = uuid4().hex
        directory = (self.root / task_id).resolve()
        if self.root not in directory.parents:
            raise ValueError("artifact path escapes configured root")
        directory.mkdir(parents=True, exist_ok=True)
        path = (directory / f"{kind}-{digest}.txt").resolve()
        if directory not in path.parents:
            raise ValueError("artifact path escapes task directory")
        if not path.exists():
            # 并发读者只能看到完整文件；崩溃不能留下被 hash 命名的半份响应。
            with NamedTemporaryFile(dir=directory, delete=False) as temporary:
                temporary.write(data)
                temporary_path = Path(temporary.name)
            try:
                os.replace(temporary_path, path)
            finally:
                temporary_path.unlink(missing_ok=True)
        return StoredArtifact(
            id=artifact_id,
            uri=path.as_uri(),
            sha256=digest,
            size_bytes=len(data),
        )

    def read_text(self, uri: str) -> str:
        if not uri.startswith("file:///"):
            raise ValueError("unsupported artifact URI")
        path = Path(unquote(uri.removeprefix("file:///"))).resolve()
        if self.root != path and self.root not in path.parents:
            raise ValueError("artifact path escapes configured root")
        with path.open(encoding="utf-8", newline="") as handle:
            return handle.read()
