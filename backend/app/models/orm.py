"""阶段 6A 持久化 ORM。

领域对象的稳定字段关系化；预算、策略结论和只读 API 聚合保留为受控 JSON 快照。
大 diff/artifact 只保存文件引用。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    BigInteger,
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PublicationAttemptOrm(Base):
    __tablename__ = "github_publication_attempts"
    __table_args__ = (UniqueConstraint("logical_key", name="uq_publication_logical"),
                      Index("ix_publication_state_time", "state", "requested_at"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    review_id: Mapped[str] = mapped_column(ForeignKey("review_tasks.id", ondelete="CASCADE"), index=True)
    repository_id: Mapped[int] = mapped_column(Integer)
    pr_number: Mapped[int] = mapped_column(Integer)
    publisher_identity: Mapped[str] = mapped_column(String(128))
    head_sha: Mapped[str] = mapped_column(String(64))
    plan_hash: Mapped[str] = mapped_column(String(64))
    selection_hash: Mapped[str] = mapped_column(String(64))
    logical_key: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), default="queued")
    plan: Mapped[dict] = mapped_column(JSON)
    selection: Mapped[dict] = mapped_column(JSON)
    context: Mapped[dict] = mapped_column(JSON)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(128))


class PublicationRequestOrm(Base):
    __tablename__ = "github_publication_requests"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    publication_id: Mapped[str] = mapped_column(ForeignKey("github_publication_attempts.id", ondelete="CASCADE"))


class PublicationItemOrm(Base):
    __tablename__ = "github_publication_items"
    __table_args__ = (UniqueConstraint("publication_id", "kind", "finding_key", name="uq_publication_item"),
                      Index("ix_publication_fingerprint", "fingerprint", "state"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    publication_id: Mapped[str] = mapped_column(ForeignKey("github_publication_attempts.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    finding_key: Mapped[str] = mapped_column(String(128), default="")
    fingerprint: Mapped[str] = mapped_column(String(64))
    payload_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(32), default="pending")
    github_comment_id: Mapped[int | None] = mapped_column(Integer)
    github_review_id: Mapped[int | None] = mapped_column(Integer)
    github_url: Mapped[str | None] = mapped_column(String(2048))
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PublicationBatchOrm(Base):
    __tablename__ = "github_publication_batches"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    publication_id: Mapped[str] = mapped_column(ForeignKey("github_publication_attempts.id", ondelete="CASCADE"), index=True)
    state: Mapped[str] = mapped_column(String(32), default="pending")
    item_ids: Mapped[list] = mapped_column(JSON)
    payload: Mapped[dict] = mapped_column(JSON)
    github_review_id: Mapped[int | None] = mapped_column(Integer)
    last_error_code: Mapped[str | None] = mapped_column(String(128))


class GitHubSummarySlotOrm(Base):
    __tablename__ = "github_summary_slots"
    __table_args__ = (UniqueConstraint("repository_id", "pr_number", "publisher_identity", name="uq_summary_slot"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    repository_id: Mapped[int] = mapped_column(Integer)
    pr_number: Mapped[int] = mapped_column(Integer)
    publisher_identity: Mapped[str] = mapped_column(String(128))
    github_comment_id: Mapped[int | None] = mapped_column(Integer)
    github_url: Mapped[str | None] = mapped_column(String(2048))
    latest_review_id: Mapped[str | None] = mapped_column(String(32))
    latest_head_sha: Mapped[str | None] = mapped_column(String(64))
    latest_order: Mapped[str] = mapped_column(String(128), default="")
    last_payload_hash: Mapped[str | None] = mapped_column(String(64))
    pending_publication_id: Mapped[str | None] = mapped_column(String(32))
    revision: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewPreviewOrm(Base):
    __tablename__ = "review_previews"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_task_id: Mapped[str | None] = mapped_column(String(32))


class PreviewJobOrm(Base):
    __tablename__ = "preview_jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    request: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    stages: Mapped[list] = mapped_column(JSON, default=list)
    preview_id: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(String(512))
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(32))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PreparedWorkspaceOrm(Base):
    __tablename__ = "prepared_workspaces"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    input_key: Mapped[str] = mapped_column(String(64), index=True)
    directory: Mapped[str] = mapped_column(String(64), unique=True)
    base_sha: Mapped[str] = mapped_column(String(64))
    head_sha: Mapped[str] = mapped_column(String(64))
    effective_base_sha: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(16), default="ready")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FindingDispositionOrm(Base):
    __tablename__ = "finding_dispositions"
    task_id: Mapped[str] = mapped_column(ForeignKey("review_tasks.id", ondelete="CASCADE"), primary_key=True)
    finding_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReviewTaskOrm(Base):
    __tablename__ = "review_tasks"
    __table_args__ = (
        UniqueConstraint("thread_id", name="uq_review_tasks_thread_id"),
        Index("ix_review_tasks_status_updated", "status", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    repository: Mapped[str | None] = mapped_column(String(512))
    repo_url: Mapped[str | None] = mapped_column(String(1024))
    pr_url: Mapped[str] = mapped_column(String(1024))
    pr_number: Mapped[int | None] = mapped_column(Integer)
    base_sha: Mapped[str | None] = mapped_column(String(64), index=True)
    head_sha: Mapped[str | None] = mapped_column(String(64), index=True)
    mode: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    current_phase: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128))
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    warnings: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text)
    task_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    task_snapshot_artifact_uri: Mapped[str | None] = mapped_column(String(1024))
    report_markdown: Mapped[str | None] = mapped_column(Text)
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)









class ReviewModelCallOrm(Base):
    __tablename__ = "review_model_calls"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("review_tasks.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[str] = mapped_column(String(160))
    reserved_input_bytes: Mapped[int] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cached_input_tokens: Mapped[int | None] = mapped_column(Integer)
    settled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReviewBranchBudgetOrm(Base):
    __tablename__ = "review_branch_budgets"
    task_id: Mapped[str] = mapped_column(ForeignKey("review_tasks.id", ondelete="CASCADE"), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    held_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    held_model_calls: Mapped[int] = mapped_column(Integer, default=0)
    finalizing: Mapped[bool] = mapped_column(Boolean, default=False)
    final_requests: Mapped[int] = mapped_column(Integer, default=0)


class ReviewCreateIntentOrm(Base):
    __tablename__ = "review_create_intents"
    __table_args__ = (UniqueConstraint("caller_id", "key", name="uq_review_create_intent"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    caller_id: Mapped[str] = mapped_column(String(128))
    key: Mapped[str] = mapped_column(String(200))
    request_hash: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[str] = mapped_column(ForeignKey("review_tasks.id"), index=True)


class ReviewWorkItemOrm(Base):
    __tablename__ = "review_work_items"
    __table_args__ = (UniqueConstraint("run_id", "kind", "subject", name="uq_review_work_subject"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("review_tasks.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(24))
    subject: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)


class ReviewCandidateOrm(Base):
    __tablename__ = "review_candidates"
    __table_args__ = (UniqueConstraint("run_id", "attempt_id", "tool_call_id",
                                     name="uq_review_candidate_tool_call"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("review_tasks.id", ondelete="CASCADE"), index=True)
    attempt_id: Mapped[str] = mapped_column(String(32))
    tool_call_id: Mapped[str] = mapped_column(String(200))
    payload: Mapped[dict] = mapped_column(JSON)


class ReviewRequestOrm(Base):
    __tablename__ = "review_requests"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("review_tasks.id", ondelete="CASCADE"), index=True)
    work_item_id: Mapped[str | None] = mapped_column(String(64))
    attempt_id: Mapped[str | None] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(24))
    reserved_input_tokens: Mapped[int] = mapped_column(Integer)
    reserved_output_tokens: Mapped[int] = mapped_column(Integer)
    usage: Mapped[dict | None] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(16), default="reserved")
    lease_fence: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkerJobOrm(Base):
    __tablename__ = "worker_jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_worker_jobs_idempotency"),
        Index("ix_worker_jobs_claim", "status", "available_at", "lease_until"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("review_tasks.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lease_owner: Mapped[str | None] = mapped_column(String(128), index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IncrementalWebhookDeliveryOrm(Base):
    """pull_request synchronize 投递幂等记录；同一 delivery 只创建一次任务。"""
    __tablename__ = "incremental_webhook_deliveries"
    __table_args__ = (Index("ix_incremental_webhook_status", "status"),)

    delivery_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    repository: Mapped[str] = mapped_column(String(512), nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)




class ArtifactOrm(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("review_tasks.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    uri: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
