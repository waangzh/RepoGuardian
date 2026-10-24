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
    LargeBinary,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

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


class PreviewInputCacheOrm(Base):
    __tablename__ = "preview_input_cache"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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
    config_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    planner_version: Mapped[str | None] = mapped_column(String(64))
    review_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    warnings: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text)
    task_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    task_snapshot_artifact_uri: Mapped[str | None] = mapped_column(String(1024))
    report_markdown: Mapped[str | None] = mapped_column(Text)
    report_artifact_uri: Mapped[str | None] = mapped_column(String(1024))
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    checkpoint_ns: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    checkpoint_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    units: Mapped[list[ReviewUnitOrm]] = relationship(cascade="all, delete-orphan")
    issues: Mapped[list[ReviewIssueOrm]] = relationship(cascade="all, delete-orphan")
    patches: Mapped[list[PatchOrm]] = relationship(cascade="all, delete-orphan")
    validations: Mapped[list[ValidationOrm]] = relationship(cascade="all, delete-orphan")
    human_requests: Mapped[list[HumanRequestOrm]] = relationship(cascade="all, delete-orphan")


class ReviewUnitOrm(Base):
    __tablename__ = "review_units"
    __table_args__ = (
        UniqueConstraint("task_id", "unit_id", name="uq_review_units_task_unit"),
        Index("ix_review_units_reuse", "fingerprint", "status", "model", "provider"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("review_tasks.id", ondelete="CASCADE"), index=True)
    unit_id: Mapped[str] = mapped_column(String(64), nullable=False)
    unit_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    budget: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    model: Mapped[str | None] = mapped_column(String(128))
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    planner_version: Mapped[str] = mapped_column(String(64), nullable=False)
    review_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    result_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    reused_from_id: Mapped[int | None] = mapped_column(ForeignKey("review_units.id"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReviewIssueOrm(Base):
    __tablename__ = "review_issues"
    __table_args__ = (UniqueConstraint("task_id", "issue_id", name="uq_review_issues_task_issue"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("review_tasks.id", ondelete="CASCADE"), index=True)
    unit_id: Mapped[int | None] = mapped_column(ForeignKey("review_units.id", ondelete="SET NULL"))
    issue_id: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    evidence_resolution: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    verifier_decision: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    dedup_source: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    evidence_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    final_status: Mapped[str] = mapped_column(String(32), nullable=False)
    publication_status: Mapped[str] = mapped_column(String(32), default="unpublished", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PatchOrm(Base):
    __tablename__ = "patches"
    __table_args__ = (
        UniqueConstraint("task_id", "patch_id", name="uq_patches_task_patch"),
        Index("ix_patches_fingerprint_status", "fingerprint", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("review_tasks.id", ondelete="CASCADE"), index=True)
    patch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    unified_diff: Mapped[str | None] = mapped_column(Text)
    diff_artifact_uri: Mapped[str | None] = mapped_column(String(1024))
    diff_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    issue_evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    policy_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    patch_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PatchIssueLinkOrm(Base):
    __tablename__ = "patch_issue_links"
    patch_id: Mapped[int] = mapped_column(ForeignKey("patches.id", ondelete="CASCADE"), primary_key=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("review_issues.id", ondelete="CASCADE"), primary_key=True)


class ValidationOrm(Base):
    __tablename__ = "validations"
    __table_args__ = (
        UniqueConstraint("task_id", "validation_id", name="uq_validations_task_validation"),
        Index("ix_validations_reuse", "fingerprint", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("review_tasks.id", ondelete="CASCADE"), index=True)
    patch_id: Mapped[int] = mapped_column(ForeignKey("patches.id", ondelete="CASCADE"), index=True)
    validation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    backend: Mapped[str] = mapped_column(String(64), nullable=False)
    request: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    environment_fingerprint: Mapped[str] = mapped_column(String(512), nullable=False)
    trust_source: Mapped[str | None] = mapped_column(String(256))
    patch_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    validation_profile: Mapped[str] = mapped_column(String(128), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class HumanRequestOrm(Base):
    __tablename__ = "human_requests"
    __table_args__ = (UniqueConstraint("task_id", "request_id", name="uq_human_task_request"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    task_id: Mapped[str] = mapped_column(ForeignKey("review_tasks.id", ondelete="CASCADE"), index=True)
    unit_id: Mapped[int | None] = mapped_column(ForeignKey("review_units.id", ondelete="SET NULL"))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    options: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    answer_option_id: Mapped[str | None] = mapped_column(String(128))
    answer_text: Mapped[str | None] = mapped_column(Text)
    answered_by: Mapped[str | None] = mapped_column(String(256))
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    checkpoint_ns: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    checkpoint_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkerJobOrm(Base):
    __tablename__ = "worker_jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_worker_jobs_idempotency"),
        Index("ix_worker_jobs_claim", "status", "available_at", "lease_until"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("review_tasks.id", ondelete="CASCADE"), index=True)
    unit_id: Mapped[int | None] = mapped_column(ForeignKey("review_units.id", ondelete="CASCADE"))
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


class SideEffectOrm(Base):
    __tablename__ = "side_effects"

    idempotency_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("review_tasks.id", ondelete="CASCADE"), index=True)
    effect_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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


class RunnerRegistrationOrm(Base):
    __tablename__ = "runner_registrations"

    runner_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    public_key: Mapped[str] = mapped_column(String(256), nullable=False)
    allowed_repositories: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    allowed_profiles: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    api_token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    encrypted_hmac_secret: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserValidationRequestOrm(Base):
    __tablename__ = "user_validation_requests"
    __table_args__ = (
        UniqueConstraint(
            "task_id", "patch_id", "profile", name="uq_user_validation_request_key"
        ),
        Index("ix_user_validation_requests_status_expiry", "status", "expires_at"),
    )

    request_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    patch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    repository_id: Mapped[str] = mapped_column(String(512), nullable=False)
    clone_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    fetch_ref: Mapped[str | None] = mapped_column(String(512))
    base_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    patch_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    patch_content: Mapped[str] = mapped_column(Text, nullable=False)
    profile: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    runner_id: Mapped[str | None] = mapped_column(
        ForeignKey("runner_registrations.runner_id", ondelete="SET NULL"), index=True
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RunnerResultIdempotencyOrm(Base):
    __tablename__ = "runner_result_idempotency"
    __table_args__ = (
        UniqueConstraint("runner_id", "idempotency_key", name="uq_runner_result_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    runner_id: Mapped[str] = mapped_column(
        ForeignKey("runner_registrations.runner_id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_id: Mapped[str] = mapped_column(
        ForeignKey("user_validation_requests.request_id", ondelete="CASCADE"), nullable=False
    )
    fingerprint: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    receipt: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProjectCIRequestOrm(Base):
    __tablename__ = "project_ci_requests"
    __table_args__ = (Index("ix_project_ci_requests_status_expiry", "status", "expires_at"),)

    request_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    patch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    repository: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    workflow: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    run_id: Mapped[int | None] = mapped_column(Integer, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProjectCIWebhookDeliveryOrm(Base):
    __tablename__ = "project_ci_webhook_deliveries"

    delivery_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_id: Mapped[str] = mapped_column(
        ForeignKey("project_ci_requests.request_id", ondelete="CASCADE"), nullable=False, index=True
    )
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
