"""Versioned, untrusted PR conversation inputs and independent recheck results."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


CONTRACT = "pr-discussion-context-v1"


class DiscussionPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: Literal["pr-discussion-context-v1"] = CONTRACT
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
    version: Literal["pr-discussion-context-v1"] = CONTRACT
    task_id: str
    pr_url: str
    head_sha: str
    base_sha: str
    started_at: str
    finished_at: str
    sources: dict[str, Literal["complete", "partial", "unavailable"]]
    limitations: list[str] = Field(default_factory=list)
    threads: list[DiscussionThread] = Field(default_factory=list)


class DiscussionReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    discussion_id: str = Field(min_length=1, max_length=256)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=4_000, ge=1, le=8_000)


class DiscussionMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    discussion_id: str
    comment_id: str
    quote: str = Field(min_length=1, max_length=4_000)
    same_subject: bool = Field(strict=True)
    same_trigger: bool = Field(strict=True)
    same_impact: bool = Field(strict=True)
    new_information: bool = Field(strict=True)
    reason: str = Field(min_length=1, max_length=2_000)


class DiscussionStats(BaseModel):
    status: Literal["not_recorded", "complete", "partial", "unavailable"] = "not_recorded"
    collected: int = 0
    comments: int = 0
    referenced: int = 0
    referenced_comments: int = 0
    checked: int = 0
    unchecked: int = 0
    limitations: list[str] = Field(default_factory=list)
    termination_reason: str | None = None


class DiscussionRecheck(BaseModel):
    discussion_id: str
    status: Literal["still_present", "not_present", "fixed", "insufficient_evidence", "not_checked", "background"]
    reason: str
    evidence_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
