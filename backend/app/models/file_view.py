"""Fixed-source responses reused by the existing Diff viewer."""

from app.review.domain import Model, Snapshot
from pydantic import ConfigDict


class FileDiffResponse(Model):
    file_id: int
    file_path: str
    old_file_path: str | None
    snapshot: Snapshot
    hunks: list[dict]
    metadata: dict
    next_cursor: int | None
    truncated: bool


class FileContextResponse(Model):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    file_id: int
    file_path: str
    snapshot: Snapshot
    base_lines: list[str]
    head_lines: list[str]
