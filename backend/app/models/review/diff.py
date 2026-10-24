"""Diff、文件变更与 PR 基础模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from app.core.paths import validate_repo_relative_path

class PullRequestRef(BaseModel):
    """PR 的 base 或 head 分支引用。"""
    ref: str
    sha: str
    repo_clone_url: str


class PullRequestInfo(BaseModel):
    """从 GitHub API 拉取的 PR 元数据。"""
    owner: str
    repo: str
    number: int
    title: str
    body: str | None = Field(default=None, max_length=65_536)
    html_url: str
    clone_url: str
    base: PullRequestRef
    head: PullRequestRef


class ChangedLine(BaseModel):
    """diff 中的一行变更。"""
    line_no: int | None
    content: str


class DiffLine(BaseModel):
    """hunk 中按原始顺序保存的双侧行。"""

    kind: Literal["added", "context", "deleted"]
    content: str
    old_line_no: int | None = None
    new_line_no: int | None = None


class DiffHunk(BaseModel):
    """diff 中的一个 hunk（连续变更块）。"""
    old_start: int
    old_length: int
    new_start: int
    new_length: int
    hunk_id: str = ""
    lines: list[DiffLine] = Field(default_factory=list)
    added_lines: list[ChangedLine] = Field(default_factory=list)
    removed_lines: list[ChangedLine] = Field(default_factory=list)


class FileChangeSide(BaseModel):
    """固定 Git tree 中的文件身份；不存在的侧没有 blob/mode/size。"""
    model_config = ConfigDict(extra="forbid")
    path: str
    blob_id: str | None = Field(default=None, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    mode: Literal["100644", "100755", "120000", "160000"] | None = None
    size: int | None = Field(default=None, ge=0)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_repo_relative_path(value)

    @model_validator(mode="after")
    def consistent_presence(self) -> "FileChangeSide":
        if (self.blob_id is None) != (self.mode is None) or (self.blob_id is None and self.size is not None):
            raise ValueError("inconsistent Git tree entry")
        if self.mode in {"100644", "100755", "120000"} and self.size is None:
            raise ValueError("blob tree entry requires size")
        return self


class FileChangeEvidence(BaseModel):
    """独立非行证据：由服务端在固定 Base/Head 上读取并核验。"""
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["file-change-evidence-v1"] = "file-change-evidence-v1"
    id: str
    file_path: str
    base_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    head_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    base: FileChangeSide
    head: FileChangeSide
    change_kinds: list[Literal["rename", "mode", "empty_added", "empty_deleted"]]
    content_hash: str


class ChangedFile(BaseModel):
    """一个文件的 diff 解析结果。"""
    file_path: str
    old_file_path: str | None = None
    change_type: str   # added / modified / deleted
    additions: int
    deletions: int
    is_binary: bool = False
    old_mode: str | None = None
    new_mode: str | None = None
    hunks: list[DiffHunk] = Field(default_factory=list)
    file_change_evidence: FileChangeEvidence | None = None

    @model_serializer(mode="wrap")
    def serialize_changed_file(self, handler):
        result = handler(self)
        for key in ("file_change_evidence", "old_mode", "new_mode"):
            if getattr(self, key) is None:
                result.pop(key, None)
        return result


class PlannedChangedFile(BaseModel):
    """Planner 对变更文件作出的确定性分类和纳入结论。"""

    file_path: str
    old_file_path: str | None = None
    change_type: str
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    classifications: list[str] = Field(default_factory=list)
    included: bool
    excluded_reason: str | None = None


class ExcludedReviewFile(BaseModel):
    file_path: str
    reason: str
    classifications: list[str] = Field(default_factory=list)


class ContextProvenance(BaseModel):
    """Planner 为文件进入 Unit 可读范围给出的可审计依据。"""

    file: str
    source: str
    distance: int = Field(ge=0, le=2)
    confidence: float = Field(ge=0, le=1)
    why_retrieved: str
    unit_id: str | None = None
