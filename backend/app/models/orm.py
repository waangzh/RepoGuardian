"""阶段 6A 持久化 ORM。

领域对象的稳定字段关系化；预算、策略结论和只读 API 聚合保留为受控 JSON 快照。
大 diff/artifact 只保存文件引用。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
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
