"""受控本地 artifact 存储；数据库只保存内容寻址引用。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.core.config import settings


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
        data = content.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        artifact_id = uuid4().hex
        directory = (self.root / task_id).resolve()
        if self.root not in directory.parents:
            raise ValueError("artifact path escapes configured root")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{kind}-{digest}.txt"
        if not path.exists():
            path.write_bytes(data)
        return StoredArtifact(
            id=artifact_id,
            uri=path.as_uri(),
            sha256=digest,
            size_bytes=len(data),
        )

    def read_text(self, uri: str) -> str:
        if not uri.startswith("file:///"):
            raise ValueError("unsupported artifact URI")
        path = Path(uri.removeprefix("file:///")).resolve()
        if self.root != path and self.root not in path.parents:
            raise ValueError("artifact path escapes configured root")
        return path.read_text(encoding="utf-8")
