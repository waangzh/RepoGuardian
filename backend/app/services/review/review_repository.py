"""Business snapshots and lifecycle fences; execution progress belongs to LangGraph."""

import json
from datetime import timedelta, timezone
from typing import Iterable

from sqlalchemy import or_, select, update

from app.core.config import settings
from app.core.database import schema_is_current, sync_session
from app.models.orm import (
    ArtifactOrm, FindingDispositionOrm, GitHubSummarySlotOrm, PublicationAttemptOrm,
    ReviewPreviewOrm, ReviewTaskOrm, WorkerJobOrm, IncrementalWebhookDeliveryOrm, utcnow,
)
from app.review.contracts import ReviewTask, ReviewTaskListResponse, TERMINAL_STATUSES
from app.services.review.artifact_store import LocalArtifactStore
from app.services.review.review_task_access import lock_live_review


class DatabaseNotMigratedError(RuntimeError):
    pass


class ReviewStopped(RuntimeError):
    pass


class ReviewLeaseLost(RuntimeError):
    pass


def as_utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class ReviewRepository:
    def __init__(self, session_factory=sync_session, artifact_store=None, *, require_migration=True):
        self._session_factory = session_factory
        self._artifacts = artifact_store or LocalArtifactStore()
        if require_migration and not schema_is_current():
            raise DatabaseNotMigratedError("database is not migrated; run `uv run alembic upgrade head`")

    def _guard(self, session, task_id, lease=None):
        if not lock_live_review(session, task_id):
            raise ReviewStopped("review_deleted")
        row = session.get(ReviewTaskOrm, task_id)
        if row.status == "cancelled":
            raise ReviewStopped("review_cancelled")
        if lease:
            job = session.get(WorkerJobOrm, lease["job_id"])
            if (job is None or job.task_id != task_id or job.status != "leased"
                    or job.lease_owner != lease["owner"] or job.attempts != lease["attempt"]
                    or job.lease_until is None or as_utc(job.lease_until) <= utcnow()):
                raise ReviewLeaseLost("review_lease_lost")
        return row

    def assert_active(self, task_id, lease=None):
        with self._session_factory.begin() as session:
            self._guard(session, task_id, lease)

    def create_task(self, task: ReviewTask, *, preview_id=None, enqueue=False):
        payload, uri = self._serialize(task)
        with self._session_factory.begin() as session:
            if preview_id:
                claimed = session.execute(update(ReviewPreviewOrm).where(
                    ReviewPreviewOrm.id == preview_id, ReviewPreviewOrm.consumed_task_id.is_(None),
                    ReviewPreviewOrm.expires_at > utcnow()).values(consumed_task_id=task.id))
                if claimed.rowcount != 1:
                    raise ValueError("review_plan_stale_or_consumed")
            session.add(ReviewTaskOrm(
                id=task.id, repository=f"{task.pr.owner}/{task.pr.repo}", repo_url=task.pr.clone_url,
                pr_url=task.pr_url, pr_number=task.pr.number, base_sha=task.snapshot.declared_base_sha,
                head_sha=task.snapshot.head_sha, mode="review", status=task.status,
                current_phase=task.phase, model=task.configuration.model,
                provider=task.configuration.provider, warnings=task.warnings, error_summary=task.error,
                task_snapshot=payload, task_snapshot_artifact_uri=uri, thread_id=task.id,
                created_at=task.created_at, updated_at=task.updated_at,
                retention_until=utcnow() + timedelta(days=settings.repoguardian_retention_days)))
            session.flush()
            if enqueue:
                from app.services.review.task_queue import DatabaseTaskQueue
                DatabaseTaskQueue(self._session_factory).enqueue(
                    task_id=task.id, idempotency_key=f"review:{task.id}", session=session)
        return task

    def save_task(self, task, *, lease=None):
        with self._session_factory.begin() as session:
            row = self._guard(session, task.id, lease)
            previous = ReviewTask.model_validate(self._load(row))
            if (previous.configuration != task.configuration or previous.snapshot != task.snapshot
                    or previous.pr != task.pr or previous.changed_files != task.changed_files):
                raise ValueError("review_inputs_are_frozen")
            payload, uri = self._serialize(task)
            row.task_snapshot, row.task_snapshot_artifact_uri = payload, uri
            row.status, row.current_phase = task.status, task.phase
            row.report_markdown, row.error_summary = task.report_markdown, task.error
            row.warnings, row.updated_at = list(task.warnings), task.updated_at
            row.completed_at = task.completed_at
            if task.status in TERMINAL_STATUSES:
                row.retention_until = utcnow() + timedelta(days=settings.repoguardian_retention_days)

    def get_task(self, task_id):
        with self._session_factory() as session:
            row = session.get(ReviewTaskOrm, task_id)
            return ReviewTask.model_validate(self._load(row)) if row and row.deleted_at is None else None

    def save_preview(self, preview_id, payload, expires_at):
        with self._session_factory.begin() as session:
            session.add(ReviewPreviewOrm(id=preview_id, payload=payload, expires_at=expires_at))

    def get_preview(self, preview_id):
        with self._session_factory() as session:
            row = session.get(ReviewPreviewOrm, preview_id)
            return {**row.payload, "consumed_task_id": row.consumed_task_id} if row else None

    def finding_dispositions(self, task_id):
        with self._session_factory() as session:
            return {row.finding_id: row.payload for row in session.scalars(select(FindingDispositionOrm)
                    .where(FindingDispositionOrm.task_id == task_id))}

    def save_finding_disposition(self, task_id, finding_id, value):
        with self._session_factory.begin() as session:
            if not lock_live_review(session, task_id):
                raise KeyError(task_id)
            row = session.get(ReviewTaskOrm, task_id)
            if row.status not in TERMINAL_STATUSES:
                raise ValueError("review_not_terminal")
            session.merge(FindingDispositionOrm(task_id=task_id, finding_id=finding_id,
                                                payload=value, updated_at=utcnow()))

    def list_tasks(self, *, status=None, page=1, page_size=20, q=None, repository=None,
                   pr_number=None, head_sha=None):
        page, page_size = max(1, page), min(100, max(1, page_size))
        filters = [ReviewTaskOrm.deleted_at.is_(None)]
        for column, value in ((ReviewTaskOrm.status, status), (ReviewTaskOrm.repository, repository),
                              (ReviewTaskOrm.pr_number, pr_number), (ReviewTaskOrm.head_sha, head_sha)):
            if value is not None:
                filters.append(column == value)
        with self._session_factory() as session:
            rows = session.scalars(select(ReviewTaskOrm).where(*filters)
                                   .order_by(ReviewTaskOrm.created_at.desc())).all()
            tasks = [ReviewTask.model_validate(self._load(row)) for row in rows]
        if q:
            tasks = [task for task in tasks if q.casefold() in
                     " ".join((task.id, task.pr_url, task.pr.title)).casefold()]
        start = (page - 1) * page_size
        return ReviewTaskListResponse(items=tasks[start:start + page_size], total=len(tasks),
                                      page=page, page_size=page_size)

    def cancel_task(self, task_id):
        with self._session_factory.begin() as session:
            if not lock_live_review(session, task_id):
                return False
            row = session.get(ReviewTaskOrm, task_id)
            if row.status in TERMINAL_STATUSES:
                return False
            row.status, row.current_phase = "cancelled", "cancelled"
            row.cancelled_at = row.completed_at = row.updated_at = utcnow()
            session.execute(update(WorkerJobOrm).where(WorkerJobOrm.task_id == task_id,
                WorkerJobOrm.kind == "review", WorkerJobOrm.status.in_(["queued", "retry", "leased"]))
                .values(status="cancelled", lease_owner=None, lease_until=None))
            return True

    def enqueue_resume(self, task_id, expected_updated_at, enqueue_key, *, retry_failed=False):
        with self._session_factory.begin() as session:
            row = self._guard(session, task_id)
            allowed = {"failed", "completed_with_warnings"} if retry_failed else {"failed"}
            if row.status not in allowed or as_utc(row.updated_at) != as_utc(expected_updated_at):
                raise ValueError("review_resume_stale")
            if session.scalar(select(WorkerJobOrm.id).where(WorkerJobOrm.task_id == task_id,
                    WorkerJobOrm.status == "leased", WorkerJobOrm.lease_until > utcnow()).limit(1)):
                raise ValueError("review_job_active")
            if session.scalar(select(PublicationAttemptOrm.id).where(
                    PublicationAttemptOrm.review_id == task_id,
                    or_(PublicationAttemptOrm.state.in_(["queued", "preparing", "publishing", "uncertain"]),
                        PublicationAttemptOrm.id.in_(select(GitHubSummarySlotOrm.pending_publication_id))))):
                raise ValueError("review_publication_pending")
            if retry_failed:
                task = ReviewTask.model_validate(self._load(row))
                if task.retry_count:
                    raise ValueError("review_retry_limit")
                task.retry_count = 1
                row.task_snapshot, row.task_snapshot_artifact_uri = self._serialize(task)
            row.status, row.current_phase, row.error_summary, row.completed_at = "queued", "prepare", None, None
            row.updated_at = utcnow()
            from app.services.review.task_queue import DatabaseTaskQueue
            DatabaseTaskQueue(self._session_factory).enqueue(task_id=task_id,
                idempotency_key=enqueue_key, session=session)

    def delete_task(self, task_id):
        return self._delete_task(task_id, require_retention=False)

    def delete_task_if_retention_elapsed(self, task_id, *, now=None):
        return self._delete_task(task_id, require_retention=True, now=now)

    def _delete_task(self, task_id, *, require_retention, now=None):
        now = now or utcnow()
        with self._session_factory.begin() as session:
            if not lock_live_review(session, task_id):
                row = session.get(ReviewTaskOrm, task_id)
                return bool(row and row.deleted_at)
            row = session.get(ReviewTaskOrm, task_id)
            if row.status not in TERMINAL_STATUSES:
                raise ValueError("only terminal tasks can be deleted")
            if require_retention and as_utc(row.retention_until) > now:
                return False
            if session.scalar(select(PublicationAttemptOrm.id).where(
                PublicationAttemptOrm.review_id == task_id,
                or_(PublicationAttemptOrm.state.in_(["queued", "preparing", "publishing", "uncertain"]),
                    PublicationAttemptOrm.id.in_(select(GitHubSummarySlotOrm.pending_publication_id))))):
                if require_retention:
                    return False
                raise ValueError("review_publication_pending")
            if session.scalar(select(WorkerJobOrm.id).where(WorkerJobOrm.task_id == task_id,
                    WorkerJobOrm.status.in_(["queued", "retry", "leased"]))):
                if require_retention:
                    return False
                raise ValueError("review_job_active")
            row.deleted_at, row.updated_at, row.status = now, now, "deleted"
            row.task_snapshot, row.task_snapshot_artifact_uri, row.report_markdown = {}, None, None
            session.execute(update(ArtifactOrm).where(ArtifactOrm.task_id == task_id,
                ArtifactOrm.deleted_at.is_(None)).values(deleted_at=now))
            return True

    def list_expired_task_ids(self, *, now=None, limit=1000):
        with self._session_factory() as session:
            return list(session.scalars(select(ReviewTaskOrm.id).where(
                ReviewTaskOrm.deleted_at.is_(None), ReviewTaskOrm.status.in_(TERMINAL_STATUSES),
                ReviewTaskOrm.retention_until <= (now or utcnow())).limit(limit)))

    def list_checkpoint_gc_thread_ids(self, thread_ids: Iterable[str]):
        candidates = list(dict.fromkeys(thread_ids))
        if not candidates:
            return []
        with self._session_factory() as session:
            rows = session.scalars(select(ReviewTaskOrm).where(
                ReviewTaskOrm.thread_id.in_(candidates), ReviewTaskOrm.status.in_(
                    ["completed", "completed_with_warnings", "cancelled", "deleted"]))).all()
            now = utcnow()
            return [row.thread_id for row in rows if row.status != "completed_with_warnings"
                or (now - as_utc(row.created_at)).total_seconds()
                    >= ReviewTask.model_validate(self._load(row)).configuration.limits.timeout_seconds]

    def _serialize(self, task):
        payload = task.model_dump(mode="json")
        encoded = json.dumps(payload, ensure_ascii=False)
        if len(encoded.encode()) <= settings.repoguardian_artifact_inline_max_bytes:
            return payload, None
        artifact = self._artifacts.put_text(task_id=task.id, kind="task-snapshot", content=encoded)
        return {"id": task.id}, artifact.uri

    def _load(self, row):
        payload = (json.loads(self._artifacts.read_text(row.task_snapshot_artifact_uri))
                   if row.task_snapshot_artifact_uri else dict(row.task_snapshot))
        payload.update(status=row.status, phase=row.current_phase, warnings=list(row.warnings),
                       error=row.error_summary, report_markdown=row.report_markdown,
                       created_at=as_utc(row.created_at), updated_at=as_utc(row.updated_at),
                       completed_at=as_utc(row.completed_at) if row.completed_at else None)
        return payload

    def claim_webhook_delivery(self, *, delivery_id, repository, pr_number, head_sha):
        from sqlalchemy.exc import IntegrityError
        try:
            with self._session_factory.begin() as session:
                session.add(IncrementalWebhookDeliveryOrm(delivery_id=delivery_id, repository=repository,
                    pr_number=pr_number, head_sha=head_sha, status="accepted"))
            return True
        except IntegrityError:
            return False

    def finish_webhook_delivery(self, delivery_id, *, status, task_id=None, reason=None):
        with self._session_factory.begin() as session:
            row = session.get(IncrementalWebhookDeliveryOrm, delivery_id)
            if row:
                row.status, row.task_id, row.reason, row.updated_at = status, task_id, reason, utcnow()

    def get_webhook_delivery(self, delivery_id):
        with self._session_factory() as session:
            row = session.get(IncrementalWebhookDeliveryOrm, delivery_id)
            return {key: getattr(row, key) for key in ("delivery_id", "repository", "pr_number",
                    "head_sha", "status", "task_id", "reason")} if row else None
