"""Frozen, untrusted PR conversation inputs."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DiscussionPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    page_size: int = 50
    timeout_seconds: int = 30
    max_roots: int = 500
    max_comments: int = 2_000
    max_bytes: int = 8 * 1024 * 1024


class DiscussionComment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=256)
    body: str
    author: str = ""
    url: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    commit_id: str | None = None
    original_commit_id: str | None = None
    line: int | None = Field(default=None, ge=1)
    original_line: int | None = Field(default=None, ge=1)
    side: str | None = None
    diff_hunk: str | None = None


class DiscussionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claims: list[str] = Field(default_factory=list, max_length=20)
    conditions: list[str] = Field(default_factory=list, max_length=20)
    objections: list[str] = Field(default_factory=list, max_length=20)
    repair_claims: list[str] = Field(default_factory=list, max_length=20)
    decisions: list[str] = Field(default_factory=list, max_length=20)
    open_questions: list[str] = Field(default_factory=list, max_length=20)
    quotes: dict[str, str] = Field(default_factory=dict, max_length=20)
    covered_comment_ids: list[str] = Field(default_factory=list, max_length=100)
    omitted_comment_ids: list[str] = Field(default_factory=list)
    partial: bool = False


class DiscussionThread(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    source: Literal["conversation", "review", "inline"]
    comments: list[DiscussionComment]
    path: str | None = None
    resolved: bool | None = None
    outdated: bool | None = None
    complete: bool = True
    summary: DiscussionSummary | None = None
    summary_key: str | None = None


class DiscussionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    pr_url: str
    head_sha: str
    base_sha: str
    started_at: str
    finished_at: str
    sources: dict[str, Literal["complete", "partial", "unavailable"]]
    limitations: list[str] = Field(default_factory=list)
    threads: list[DiscussionThread] = Field(default_factory=list)
