"""GitHub 文件概览，不提供正式方案凭据或仓库索引。"""
from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class QuickPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pr_url: HttpUrl


class QuickPreviewFile(BaseModel):
    path: str
    previous_path: str | None = None
    status: str
    additions: int = Field(ge=0, strict=True)
    deletions: int = Field(ge=0, strict=True)


class QuickPreviewResponse(BaseModel):
    pr_url: str
    repository: str
    number: int
    title: str
    head_sha: str
    declared_base_sha: str
    files: list[QuickPreviewFile] = Field(max_length=100)
    files_complete: bool
    requires_full_preview: bool = True
