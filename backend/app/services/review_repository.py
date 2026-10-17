"""审查任务的持久化仓储与阶段 6A 复用策略。"""

from __future__ import annotations

import json
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from uuid import uuid4

from sqlalchemy import delete, func, select, update, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import schema_is_current, sync_session
from app.models.orm import (
    ArtifactOrm,
    HumanRequestOrm,
    PatchIssueLinkOrm,
    PatchOrm,
    ReviewIssueOrm,
    ReviewTaskOrm,
    ReviewUnitOrm,
    SideEffectOrm,
    ValidationOrm,
    WorkerJobOrm,
    PublicationAttemptOrm,
    GitHubSummarySlotOrm,
    utcnow,
    ReviewPreviewOrm,
    FindingDispositionOrm,
)
from app.models.persistence import (
    HumanRequestAnswer,
    HumanRequestDetail,
    HumanRequestOption,
    HumanRequestStatus,
    PatchDetail,
    ReviewIssueDetail,
    ReviewTaskListResponse,
    ReviewUnitDetail,
    ValidationDetail,
)
from app.models.review import (
    IssueStatus,
    ReviewIssue,
    ReviewTask,
    ReviewUnit,
    ReviewUnitResult,
    ReviewUnitStatus,
    TaskStatus,
)
from app.services.artifact_store import LocalArtifactStore
from app.services.fingerprints import patch_fingerprint, stable_hash, validation_fingerprint
from app.services.review_planner import PLANNER_VERSION
from app.review.unit_completion import is_reusable_review_unit_result

TERMINAL_TASK_STATUSES = {
    TaskStatus.completed.value,
    TaskStatus.completed_with_warnings.value,
    TaskStatus.failed.value,
    TaskStatus.cancelled.value,
}


class DatabaseNotMigratedError(RuntimeError):
    pass


def _runtime_integrity_error(exc: IntegrityError, operation: str) -> BaseException:
    """只将 SQLite SideEffect 幂等主键竞争视为失租，其他约束错误脱敏传播。"""
    from app.services.coordination_runtime import CoordinationLeaseLost

    code = getattr(exc.orig, "sqlite_errorcode", None)
    if (isinstance(exc.orig, sqlite3.IntegrityError)
            and code == sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY
            and str(exc.orig) == "UNIQUE constraint failed: side_effects.idempotency_key"):
        return CoordinationLeaseLost(f"{operation} concurrent writer")
    names = {
        sqlite3.SQLITE_CONSTRAINT_UNIQUE: "SQLITE_CONSTRAINT_UNIQUE",
        sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY: "SQLITE_CONSTRAINT_PRIMARYKEY",
        sqlite3.SQLITE_CONSTRAINT_FOREIGNKEY: "SQLITE_CONSTRAINT_FOREIGNKEY",
        sqlite3.SQLITE_CONSTRAINT_NOTNULL: "SQLITE_CONSTRAINT_NOTNULL",
        sqlite3.SQLITE_CONSTRAINT_CHECK: "SQLITE_CONSTRAINT_CHECK",
    }
    detail = names.get(code, "INTEGRITY_ERROR") if isinstance(code, int) else "INTEGRITY_ERROR"
    # 原异常包含 SQL/JSON 参数，不能进入 Service 的 str(exc) 或 traceback。
    return IntegrityError(None, None, sqlite3.IntegrityError(
        f"{operation}: database integrity violation [{detail}]"), hide_parameters=True)


class ReviewRepository:
    def __init__(
        self,
        session_factory=sync_session,
        artifact_store: LocalArtifactStore | None = None,
        *,
        require_migration: bool = True,
    ) -> None:
        self._session_factory = session_factory
        self._artifacts = artifact_store or LocalArtifactStore()
        if require_migration and not schema_is_current():
            raise DatabaseNotMigratedError(
                "database is not migrated; run `cd backend; uv run alembic upgrade head`"
            )

    def create_task(self, task: ReviewTask, *, preview_id: str | None = None,
                    enqueue: bool = False) -> ReviewTask:
        now = utcnow()
        snapshot, snapshot_uri = self._serialize_task_snapshot(task)
        with self._session_factory.begin() as session:
            if preview_id:
                claimed = session.execute(update(ReviewPreviewOrm).where(
                    ReviewPreviewOrm.id == preview_id,
                    ReviewPreviewOrm.consumed_task_id.is_(None),
                    ReviewPreviewOrm.expires_at > now,
                ).values(consumed_task_id=task.id))
                if claimed.rowcount != 1:
                    raise ValueError("review_plan_stale_or_consumed")
            row = ReviewTaskOrm(
                id=task.id,
                repository=_repository_from_pr_url(task.pr_url),
                repo_url=None,
                pr_url=task.pr_url,
                pr_number=None,
                mode=task.mode.value,
                # engine is persisted in the immutable task snapshot; old rows default on read.
                status=task.status.value,
                current_phase=task.phase.value,
                model=task.model or settings.repoguardian_model,
                provider=settings.repoguardian_provider,
                config_version=settings.repoguardian_config_version,
                prompt_version=settings.repoguardian_prompt_version,
                rule_version=settings.repoguardian_rule_version,
                tool_schema_version=settings.repoguardian_tool_schema_version,
                planner_version=PLANNER_VERSION,
                review_policy_version=settings.repoguardian_review_policy_version,
                warnings=list(task.warnings),
                error_summary=task.error,
                task_snapshot=snapshot,
                task_snapshot_artifact_uri=snapshot_uri,
                report_markdown=task.report_markdown,
                thread_id=task.id,
                checkpoint_ns="",
                created_at=task.created_at,
                updated_at=task.updated_at,
                retention_until=now + timedelta(days=settings.repoguardian_retention_days),
            )
            session.add(row)
            if enqueue:
                from app.services.task_queue import DatabaseTaskQueue
                session.flush()
                DatabaseTaskQueue(self._session_factory).enqueue(task_id=task.id,
                    idempotency_key=f"review:{task.id}:initial", session=session)
            self._record_artifact(session, task.id, "task-snapshot", snapshot_uri)
        return task

    def save_preview(self, preview_id, payload, expires_at):
        with self._session_factory.begin() as session:
            session.add(ReviewPreviewOrm(id=preview_id, payload=payload, expires_at=expires_at))

    def get_preview(self, preview_id):
        with self._session_factory() as session:
            row = session.get(ReviewPreviewOrm, preview_id)
            return {**row.payload, "consumed_task_id": row.consumed_task_id} if row else None

    def finding_dispositions(self, task_id):
        with self._session_factory() as session:
            return {row.finding_id: {**row.payload, "updated_at": row.updated_at}
                    for row in session.scalars(select(FindingDispositionOrm).where(
                        FindingDispositionOrm.task_id == task_id))}

    def save_finding_disposition(self, task_id, finding_id, value):
        with self._session_factory.begin() as session:
            session.merge(FindingDispositionOrm(task_id=task_id, finding_id=finding_id,
                                                payload=value, updated_at=utcnow()))

    def save_task(self, task: ReviewTask, *, checkpoint_id: str | None = None,
                  lease: dict | None = None) -> None:
        snapshot, snapshot_uri = self._serialize_task_snapshot(task)
        now = utcnow()
        with self._session_factory.begin() as session:
            row = session.get(ReviewTaskOrm, task.id)
            if row is None:
                raise KeyError(task.id)
            if lease:
                job = session.get(WorkerJobOrm, lease["job_id"])
                if (job is None or job.task_id != task.id or job.status != "leased"
                        or job.lease_owner != lease["owner"] or job.attempts != lease["attempt"]
                        or job.lease_until is None
                        or job.lease_until.replace(tzinfo=timezone.utc) <= now):
                    return  # 旧执行者不能将新执行中的任务改为等待重试或失败。
            persisted_snapshot = self._load_task_snapshot(row)
            persisted_engine = persisted_snapshot.get("engine", "legacy_units")
            if task.engine != persisted_engine:
                raise ValueError("review engine is frozen for the lifetime of a task")
            if persisted_snapshot.get("task_runtime_configuration") != task.task_runtime_configuration:
                raise ValueError("task runtime configuration is frozen for the lifetime of a task")
            if row.status == TaskStatus.cancelled.value and task.status != TaskStatus.cancelled:
                return  # 迟到的进度/worker 结果不能覆盖用户取消。
            row.status = task.status.value
            row.current_phase = task.phase.value
            row.mode = task.mode.value
            row.model = task.model or settings.repoguardian_model
            row.warnings = list(task.warnings)
            row.error_summary = task.error
            row.task_snapshot = snapshot
            row.task_snapshot_artifact_uri = snapshot_uri
            row.report_markdown = task.report_markdown
            row.updated_at = task.updated_at
            row.checkpoint_id = checkpoint_id or row.checkpoint_id
            if task.pr:
                row.repository = f"{task.pr.owner}/{task.pr.repo}"
                row.repo_url = task.pr.clone_url
                row.pr_number = task.pr.number
                row.base_sha = task.pr.base.sha
                row.head_sha = task.pr.head.sha
            if task.status.value in TERMINAL_TASK_STATUSES and row.completed_at is None:
                row.completed_at = now
                row.retention_until = now + timedelta(
                    days=settings.repoguardian_retention_days
                )
            if task.status == TaskStatus.cancelled:
                row.cancelled_at = row.cancelled_at or now
            self._record_artifact(session, task.id, "task-snapshot", snapshot_uri)
            self._sync_units(session, row, task)
            self._sync_issues(session, row, task)
            self._sync_patches(session, row, task)
            self._sync_validations(session, row, task)

    def get_task(self, task_id: str) -> ReviewTask | None:
        with self._session_factory() as session:
            row = session.get(ReviewTaskOrm, task_id)
            if row is None or row.deleted_at is not None:
                return None
            snapshot = self._load_task_snapshot(row)
            return ReviewTask.model_validate(snapshot)

    def list_tasks(
        self, *, status: str | None = None, page: int = 1, page_size: int = 20,
        q: str | None = None, repository: str | None = None,
        pr_number: int | None = None, head_sha: str | None = None,
    ) -> ReviewTaskListResponse:
        page = max(page, 1)
        page_size = min(max(page_size, 1), 100)
        with self._session_factory() as session:
            filters = [ReviewTaskOrm.deleted_at.is_(None)]
            if status:
                filters.append(ReviewTaskOrm.status == status)
            if q:
                filters.append(or_(ReviewTaskOrm.id.contains(q, autoescape=True),
                    ReviewTaskOrm.pr_url.icontains(q, autoescape=True),
                    ReviewTaskOrm.task_snapshot["pr"]["title"].as_string().icontains(q, autoescape=True),
                    ReviewTaskOrm.task_snapshot_artifact_uri.is_not(None),
                    ReviewTaskOrm.repository.icontains(q, autoescape=True)))
            if repository:
                filters.append(ReviewTaskOrm.repository == repository)
            if pr_number is not None:
                filters.append(or_(ReviewTaskOrm.pr_number == pr_number,
                    ReviewTaskOrm.pr_url.endswith(f"/pull/{pr_number}")))
            if head_sha:
                filters.append(ReviewTaskOrm.head_sha == head_sha)
            if q:
                # 旧外置快照的数据库 stub 没有标题，必须在服务端读取真实标题后再分页。
                candidates = session.scalars(select(ReviewTaskOrm).where(*filters)
                    .order_by(ReviewTaskOrm.created_at.desc())).all()
                matched = []
                for row in candidates:
                    payload = self._load_task_snapshot(row)
                    title = (payload.get("pr") or {}).get("title") or ""
                    text = " ".join([row.id, row.pr_url, row.repository or "", title])
                    if q.casefold() in text.casefold():
                        matched.append(payload)
                start = (page - 1) * page_size
                return ReviewTaskListResponse(items=[ReviewTask.model_validate(item)
                    for item in matched[start:start + page_size]], total=len(matched), page=page, page_size=page_size)
            total = session.scalar(select(func.count()).select_from(ReviewTaskOrm).where(*filters)) or 0
            rows = session.scalars(
                select(ReviewTaskOrm)
                .where(*filters)
                .order_by(ReviewTaskOrm.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            return ReviewTaskListResponse(
                items=[ReviewTask.model_validate(self._load_task_snapshot(row)) for row in rows],
                total=total,
                page=page,
                page_size=page_size,
            )

    def get_unit(self, task_id: str, unit_id: str) -> ReviewUnitDetail | None:
        with self._session_factory() as session:
            row = session.scalar(select(ReviewUnitOrm).where(
                ReviewUnitOrm.task_id == task_id, ReviewUnitOrm.unit_id == unit_id
            ))
            if row is None:
                return None
            return ReviewUnitDetail(
                unit=ReviewUnit.model_validate(row.unit_snapshot),
                result=ReviewUnitResult.model_validate(row.result_snapshot) if row.result_snapshot else None,
                attempts=row.attempts,
                reused_from_id=row.reused_from_id,
                started_at=row.started_at,
                finished_at=row.finished_at,
                failure_reason=row.failure_reason,
            )

    def get_issue(self, task_id: str, issue_id: str) -> ReviewIssueDetail | None:
        with self._session_factory() as session:
            task_row = session.get(ReviewTaskOrm, task_id)
            if task_row is None or task_row.deleted_at is not None:
                return None
            snapshot = self._load_task_snapshot(task_row)
            if not any(
                str(item.get("id")) == issue_id for item in snapshot.get("issues") or []
            ):
                return None
            row = session.scalar(select(ReviewIssueOrm).where(
                ReviewIssueOrm.task_id == task_id, ReviewIssueOrm.issue_id == issue_id
            ))
            if row is None:
                return None
            payload = dict(row.candidate)
            payload["status"] = row.final_status
            return ReviewIssueDetail(
                issue=ReviewIssue.model_validate(payload),
                evidence_resolution=row.evidence_resolution,
                verifier_decision=row.verifier_decision,
                dedup_source=row.dedup_source,
                publication_status=row.publication_status,
            )

    def get_patch(self, task_id: str, patch_id: str) -> PatchDetail | None:
        with self._session_factory() as session:
            row = session.scalar(select(PatchOrm).where(
                PatchOrm.task_id == task_id, PatchOrm.patch_id == patch_id
            ))
            if row is None:
                return None
            payload = dict(row.patch_snapshot)
            if row.unified_diff is None and row.diff_artifact_uri:
                payload["diff_content"] = self._artifacts.read_text(row.diff_artifact_uri)
            return PatchDetail(
                patch=payload,
                diff_artifact_uri=row.diff_artifact_uri,
                fingerprint=row.fingerprint,
                revision=row.revision,
                policy_result=row.policy_result,
            )

    def get_validation(self, task_id: str, validation_id: str) -> ValidationDetail | None:
        with self._session_factory() as session:
            row = session.scalar(select(ValidationOrm).where(
                ValidationOrm.task_id == task_id,
                ValidationOrm.validation_id == validation_id,
            ))
            if row is None:
                return None
            patch = session.get(PatchOrm, row.patch_id)
            return ValidationDetail(
                validation_id=row.validation_id,
                task_id=row.task_id,
                patch_id=patch.patch_id if patch else "",
                backend=row.backend,
                request=row.request,
                result=row.result,
                environment_fingerprint=row.environment_fingerprint,
                trust_source=row.trust_source,
                patch_hash=row.patch_hash,
                fingerprint=row.fingerprint,
                status=row.status,
                requested_at=row.requested_at,
                completed_at=row.completed_at,
            )

    def find_reusable_unit(
        self,
        *,
        fingerprint: str,
        model: str,
        provider: str,
        prompt_version: str,
        rule_version: str,
        tool_schema_version: str,
        review_policy_version: str,
    ) -> ReviewUnitOrm | None:
        filters = [
            ReviewUnitOrm.fingerprint == fingerprint,
            ReviewUnitOrm.status == ReviewUnitStatus.completed.value,
            ReviewUnitOrm.provider == provider,
            ReviewUnitOrm.prompt_version == prompt_version,
            ReviewUnitOrm.rule_version == rule_version,
            ReviewUnitOrm.tool_schema_version == tool_schema_version,
            ReviewUnitOrm.review_policy_version == review_policy_version,
        ]
        if not settings.repoguardian_allow_cross_model_reuse:
            filters.append(ReviewUnitOrm.model == model)
        with self._session_factory() as session:
            rows = session.scalars(
                select(ReviewUnitOrm).where(*filters).order_by(ReviewUnitOrm.finished_at.desc())
            )
            for row in rows:
                if row.unit_snapshot.get("grouping_reason") == "cross_unit_followup":
                    continue
                if not row.result_snapshot:
                    continue
                result = ReviewUnitResult.model_validate(row.result_snapshot)
                if is_reusable_review_unit_result(result):
                    return row
            return None

    def record_unit_result(
        self,
        *,
        task_id: str,
        unit: ReviewUnit,
        result: ReviewUnitResult,
        reused_from_id: int | None = None,
    ) -> None:
        if unit.grouping_reason == "cross_unit_followup":
            raise ValueError("followup results must use the isolated coordination runtime")
        now = utcnow()
        with self._session_factory.begin() as session:
            task = session.get(ReviewTaskOrm, task_id)
            if task is None or task.status == TaskStatus.cancelled.value:
                raise ValueError("cancelled or missing task cannot accept Unit results")
            row = session.scalar(select(ReviewUnitOrm).where(
                ReviewUnitOrm.task_id == task_id,
                ReviewUnitOrm.unit_id == unit.id,
            ))
            if row is None:
                row = ReviewUnitOrm(
                    task_id=task_id,
                    unit_id=unit.id,
                    unit_snapshot=unit.model_dump(mode="json"),
                    fingerprint=unit.fingerprint,
                    status=result.status.value,
                    model=task.model,
                    provider=task.provider,
                    prompt_version=task.prompt_version,
                    rule_version=task.rule_version,
                    tool_schema_version=task.tool_schema_version,
                    planner_version=task.planner_version or PLANNER_VERSION,
                    review_policy_version=task.review_policy_version,
                )
                session.add(row)
            row.unit_snapshot = unit.model_dump(mode="json")
            row.fingerprint = unit.fingerprint
            row.status = result.status.value
            row.result_snapshot = result.model_dump(mode="json")
            row.budget = result.execution_budget.model_dump(mode="json")
            row.reused_from_id = reused_from_id
            row.attempts = (row.attempts or 0) + (0 if reused_from_id else 1)
            row.started_at = row.started_at or now
            row.finished_at = now
            row.failure_reason = result.error
            row.updated_at = now
            session.flush()
            for issue in result.issues:
                issue_row = session.scalar(select(ReviewIssueOrm).where(
                    ReviewIssueOrm.task_id == task_id,
                    ReviewIssueOrm.issue_id == issue.id,
                ))
                payload = issue.model_dump(mode="json")
                evidence = payload.get("primary_evidence") or {}
                if issue_row is None:
                    issue_row = ReviewIssueOrm(
                        task_id=task_id,
                        unit_id=row.id,
                        issue_id=issue.id,
                        candidate=payload,
                        final_status=issue.status.value,
                    )
                    session.add(issue_row)
                issue_row.candidate = payload
                issue_row.evidence_resolution = evidence
                issue_row.evidence_hash = evidence.get("anchor_hash") or stable_hash(evidence)
                issue_row.final_status = issue.status.value
                issue_row.updated_at = now

    def save_issue_lifecycle(self, task_id: str, state: dict[str, Any]) -> None:
        checks = {
            str(item.get("issue_id")): item
            for item in state.get("deterministic_issue_checks") or []
        }
        verifications = {
            str(item.get("issue_id")): item
            for item in state.get("issue_verifications") or []
        }
        dedup_by_issue: dict[str, dict[str, Any]] = {}
        for decision in state.get("issue_deduplication_decisions") or []:
            dedup_by_issue[str(decision.get("canonical_issue_id"))] = decision
            for duplicate in decision.get("duplicate_issue_ids") or []:
                dedup_by_issue[str(duplicate)] = decision
        current = {
            str(item.get("id")): item for item in state.get("review_issues") or []
        }
        with self._session_factory.begin() as session:
            rows = session.scalars(
                select(ReviewIssueOrm).where(ReviewIssueOrm.task_id == task_id)
            ).all()
            for row in rows:
                if row.issue_id in current:
                    payload = current[row.issue_id]
                    row.candidate = payload
                    row.final_status = str(payload.get("status") or row.final_status)
                    row.evidence_resolution = payload.get("primary_evidence")
                check = checks.get(row.issue_id)
                verification = verifications.get(row.issue_id)
                row.verifier_decision = {
                    "deterministic": check,
                    "verifier": verification,
                }
                row.dedup_source = dedup_by_issue.get(row.issue_id) or row.dedup_source
                if check and not check.get("passed"):
                    row.final_status = IssueStatus.dismissed.value
                if verification and verification.get("decision") == "drop":
                    row.final_status = IssueStatus.dismissed.value
                row.updated_at = utcnow()

    def get_human_request(self, task_id: str, request_id: str) -> HumanRequestDetail | None:
        with self._session_factory() as session:
            row = session.scalar(select(HumanRequestOrm).where(
                HumanRequestOrm.task_id == task_id,
                HumanRequestOrm.request_id == request_id,
            ))
            return self._human_detail(row) if row else None

    def list_human_requests(self, task_id: str) -> list[HumanRequestDetail]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(HumanRequestOrm)
                .where(HumanRequestOrm.task_id == task_id)
                .order_by(HumanRequestOrm.created_at)
            ).all()
            return [self._human_detail(row) for row in rows]

    def cancel_task(self, task_id: str) -> bool:
        now = utcnow()
        with self._session_factory.begin() as session:
            task = session.get(ReviewTaskOrm, task_id)
            if task is None or task.status in TERMINAL_TASK_STATUSES:
                return False
            task.status = TaskStatus.cancelled.value
            task.current_phase = "failed"
            task.cancelled_at = now
            task.completed_at = now
            task.retention_until = now + timedelta(days=settings.repoguardian_retention_days)
            task.updated_at = now
            payload = self._load_task_snapshot(task)
            if payload.get("engine") == "task_review_v1":
                self._cancel_task_runtime(session, task, payload)
            session.execute(update(WorkerJobOrm).where(
                WorkerJobOrm.task_id == task_id,
                WorkerJobOrm.kind.in_(["review", "unit_retry"]),
                WorkerJobOrm.status.in_(["queued", "retry", "leased"]),
            ).values(status="cancelled", lease_owner=None, lease_until=None, updated_at=now))
            session.execute(update(HumanRequestOrm).where(
                HumanRequestOrm.task_id == task_id,
                HumanRequestOrm.status == HumanRequestStatus.pending.value,
            ).values(status=HumanRequestStatus.cancelled.value, updated_at=now))
            return True

    def _cancel_task_runtime(self, session: Session, task: ReviewTaskOrm, payload: dict) -> None:
        """用户取消事务持有写权限；旧 worker 此后只能被拒绝，不能免费释放在途。"""
        from app.services.review_run_budget import ReviewRunBudget, ObligationState
        from app.models.review_runtime import TaskRuntimeReport
        budget_row = session.get(SideEffectOrm, f"run-budget:{task.id}")
        budget = ReviewRunBudget.restore(budget_row.result) if budget_row and budget_row.result else None
        if budget:
            for obligation in budget.obligations.values():
                if obligation.state == ObligationState.ACTIVE:
                    obligation.state = ObligationState.ABANDONED
            budget.revision += 1
            budget_row.result = budget.snapshot()
        runtime_row = session.get(SideEffectOrm, f"task-review-v1:{task.id}")
        runtime = dict(runtime_row.result) if runtime_row and runtime_row.result else {}
        from copy import deepcopy
        runtime = deepcopy(runtime)
        examinations, investigations, findings, leads, hints = {}, [], [], [], []
        def read_reference(ref):
            content = self._artifacts.read_text(ref["uri"])
            if hashlib.sha256(content.encode()).hexdigest() != ref["sha256"]:
                raise ValueError("cancelled runtime artifact invalid")
            return json.loads(content)
        for identity, reference in runtime.get("sessions", {}).items():
            try:
                saved = read_reference(reference)
            except (OSError, ValueError, KeyError):
                continue
            examinations.update(saved.get("examinations", {}))
            if saved.get("investigation"):
                answer = saved["investigation"]
                investigations.append({**answer, "work_item_id": identity, "status": "answered" if answer["answer"] != "insufficient_evidence"
                                       else "insufficient_evidence"})
        if runtime.get("derivation"):
            try:
                derived = read_reference(runtime["derivation"])
                findings = list(derived.get("candidates", {}).values())
                leads = [{"lead_id": key, **value} for key, value in derived.get("leads", {}).items()
                         if value.get("status") == "not_scheduled"]
                hints = [{"lead_id": key, **value} for key, value in derived.get("leads", {}).items()
                         if value.get("status") == "hint"]
                from app.services.task_review_projection import relationship_leads_for_item
                for investigation in investigations:
                    investigation["relationship_leads"] = relationship_leads_for_item(
                        derived.get("leads", {}), investigation["work_item_id"])
            except (OSError, ValueError, KeyError):
                pass
        outcomes = [{**goal, "status": "unsupported" if goal.get("unsupported_reason") else
                     examinations[key]["disposition"] if key in examinations else "not_examined"}
                    for key, goal in runtime.get("goals", {}).items() if goal.get("origin") == "initial"]
        attempts = list(budget.attempts.values()) if budget else []
        unknown = sum(attempt.sent and not attempt.usage_available for attempt in attempts)
        for item in runtime.get("items", []):
            if item["state"] != "completed":
                item["state"] = "unknown" if item["state"] == "running" else "cancelled"
        report = TaskRuntimeReport(execution_status="cancelled", termination_reason="cancelled",
            scope_complete=bool(outcomes) and all(value["status"] == "examined" for value in outcomes),
            initial_scope_coverage=outcomes, admitted_investigations=investigations, findings=findings,
            unscheduled_leads=leads, relationship_hints=hints, usage_integrity="unknown" if unknown else "complete",
            sent_calls=sum(attempt.sent for attempt in attempts), settled_tokens=budget.settled_actual if budget else 0,
            unknown_attempts=unknown, total_tokens=None if unknown else budget.settled_actual if budget else 0,
            work_items=runtime.get("items", []))
        payload["task_runtime_report"] = report.model_dump(mode="json")
        snapshot, uri = self._serialize_task_snapshot(ReviewTask.model_validate(payload))
        task.task_snapshot, task.task_snapshot_artifact_uri = snapshot, uri
        self._record_artifact(session, task.id, "task-snapshot", uri)
        if runtime_row:
            runtime.update(cancelled=True, revision=runtime["revision"] + 1)
            runtime_row.result = runtime
            runtime_row.status = "cancelled"

    def delete_task_if_retention_elapsed(
        self, task_id: str, *, now: datetime | None = None
    ) -> bool:
        now = now or utcnow()
        with self._session_factory.begin() as session:
            task = session.get(ReviewTaskOrm, task_id)
            if task is None:
                return False
            if task.deleted_at is not None:
                return True
            if task.status not in TERMINAL_TASK_STATUSES:
                raise ValueError("only terminal tasks can be deleted")
            if _as_utc(task.retention_until) > now:
                raise ValueError("task retention period has not elapsed")
            if session.scalar(select(PublicationAttemptOrm.id).where(
                    PublicationAttemptOrm.review_id == task_id,
                    or_(PublicationAttemptOrm.state.in_(["queued", "preparing", "publishing", "uncertain"]),
                        PublicationAttemptOrm.id.in_(select(GitHubSummarySlotOrm.pending_publication_id))))):
                return False  # 未确认的外部副作用需要保留任务与对账证据。
            task.deleted_at = now
            task.status = "deleted"
            task.task_snapshot = {"id": task.id, "deleted": True}
            task.task_snapshot_artifact_uri = None
            task.report_markdown = None
            session.execute(update(ArtifactOrm).where(
                ArtifactOrm.task_id == task_id,
                ArtifactOrm.deleted_at.is_(None),
            ).values(deleted_at=now))
            return True

    def list_checkpoint_gc_thread_ids(self, thread_ids: Iterable[str]) -> list[str]:
        """列出已不再需要图恢复能力的终态任务 thread。"""
        candidates = list(dict.fromkeys(thread_ids))
        if not candidates:
            return []
        eligible: list[str] = []
        with self._session_factory() as session:
            for offset in range(0, len(candidates), 500):
                batch = candidates[offset : offset + 500]
                eligible.extend(session.scalars(
                    select(ReviewTaskOrm.thread_id).where(
                        ReviewTaskOrm.thread_id.in_(batch),
                        ReviewTaskOrm.status.in_([*TERMINAL_TASK_STATUSES, "deleted"]),
                    )
                ))
        return eligible

    def list_expired_task_ids(
        self, *, now: datetime | None = None, limit: int = 1_000
    ) -> list[str]:
        """列出保留期已结束、可执行软删除的终态任务。"""
        now = now or utcnow()
        with self._session_factory() as session:
            return list(session.scalars(
                select(ReviewTaskOrm.id).where(
                    ReviewTaskOrm.deleted_at.is_(None),
                    ReviewTaskOrm.status.in_(TERMINAL_TASK_STATUSES),
                    ReviewTaskOrm.retention_until <= now,
                ).limit(limit)
            ))

    def load_coordination_runtime(self, task_id: str, fingerprint: str) -> tuple[dict | None, dict | None]:
        with self._session_factory() as session:
            row = session.get(SideEffectOrm, f"cross-unit:{task_id}:{fingerprint}")
            budget = session.get(SideEffectOrm, f"cross-unit-budget:{task_id}")
            return (dict(row.result) if row and row.result else None,
                    dict(budget.result["budget"]) if budget and budget.result else None)

    def load_run_budget(self, task_id: str) -> dict | None:
        with self._session_factory() as session:
            row = session.get(SideEffectOrm, f"run-budget:{task_id}")
            return dict(row.result) if row and row.result else None

    def load_task_review_runtime(self, task_id: str) -> dict | None:
        with self._session_factory() as session:
            row = session.get(SideEffectOrm, f"task-review-v1:{task_id}")
            return dict(row.result) if row and row.result else None

    def save_task_review_runtime(self, task_id: str, payload: dict, expected_revision: int,
                                 lease: dict | None = None) -> None:
        """新引擎独立 namespace；任何取消/过期 lease 都不能继续发布。"""
        from app.services.coordination_runtime import CoordinationLeaseLost
        if (payload.get("version") != "task-review-runtime-v1"
                or payload.get("spec", {}).get("run_id") != task_id
                or payload.get("revision") != expected_revision + 1):
            raise ValueError("invalid task runtime revision or identity")
        try:
            with self._session_factory.begin() as session:
                task = session.get(ReviewTaskOrm, task_id)
                if task is None or task.deleted_at is not None:
                    raise CoordinationLeaseLost("missing task review run")
                # 引擎由持久化任务快照冻结，运行 spec 不能升级旧任务。
                snapshot = self._load_task_snapshot(task)
                if snapshot.get("engine", "legacy_units") != "task_review_v1":
                    raise ValueError("task runtime engine mismatch")
                if lease:
                    job = session.get(WorkerJobOrm, lease["job_id"])
                    if (job is None or job.task_id != task_id or job.status != "leased"
                            or job.lease_owner != lease["owner"] or job.attempts != lease["attempt"]
                            or job.lease_until is None
                            or job.lease_until.replace(tzinfo=timezone.utc) <= utcnow()):
                        raise CoordinationLeaseLost("task review worker lease lost")
                if task.status == TaskStatus.cancelled.value and not payload.get("cancelled"):
                    raise CoordinationLeaseLost("task review cancelled")
                key = f"task-review-v1:{task_id}"
                row = session.get(SideEffectOrm, key)
                if row is None:
                    if expected_revision != -1:
                        raise CoordinationLeaseLost("task runtime missing")
                    session.add(SideEffectOrm(idempotency_key=key, task_id=task_id,
                        effect_type="task-review-runtime-v1", target=task_id,
                        status="cancelled" if payload.get("cancelled") else "active",
                        request_hash=stable_hash(payload["spec"]), result=payload))
                else:
                    if (row.result["spec"] != payload["spec"] or row.result["goals"] != payload["goals"]
                            or any(payload["pages"].get(key) != value for key, value in row.result["pages"].items())
                            or row.result.get("derivation_config_hash") != payload.get("derivation_config_hash")
                            or any(payload.get("derived_goals", {}).get(key) != value
                                   for key, value in row.result.get("derived_goals", {}).items())):
                        raise ValueError("task runtime frozen manifest mismatch")
                    changed = session.execute(update(SideEffectOrm).where(
                        SideEffectOrm.idempotency_key == key,
                        SideEffectOrm.result["revision"].as_integer() == expected_revision,
                    ).values(result=payload, status="cancelled" if payload.get("cancelled") else "active"))
                    if changed.rowcount != 1:
                        raise CoordinationLeaseLost("task runtime revision changed")
                references = list(payload.get("sessions", {}).values())
                if payload.get("derivation"):
                    references.append(payload["derivation"])
                references.extend(call["response"] for call in payload.get("calls", {}).values()
                                  if call.get("response"))
                for ref in references:
                    content = self._artifacts.read_text(ref["uri"])
                    if (hashlib.sha256(content.encode("utf-8")).hexdigest() != ref["sha256"]
                            or len(content.encode("utf-8")) != ref["size_bytes"]):
                        raise ValueError("task runtime artifact hash mismatch")
                    self._record_artifact(session, task_id, "task-review-v1", ref["uri"])
        except IntegrityError as exc:
            raise _runtime_integrity_error(exc, "task runtime") from None

    def save_run_budget(self, task_id: str, payload: dict, expected_revision: int,
                        lease: dict | None = None) -> None:
        """以 SideEffect CAS 持久化运行预算；旧 worker 无法覆盖新 revision。"""
        from app.services.coordination_runtime import CoordinationLeaseLost
        try:
            with self._session_factory.begin() as session:
                task = session.get(ReviewTaskOrm, task_id)
                if task is None or task.deleted_at is not None:
                    raise CoordinationLeaseLost("missing review run")
                if lease:
                    job = session.get(WorkerJobOrm, lease["job_id"])
                    if (job is None or job.status != "leased" or job.lease_owner != lease["owner"]
                            or job.attempts != lease["attempt"] or job.lease_until is None
                            or job.lease_until.replace(tzinfo=timezone.utc) <= utcnow()):
                        raise CoordinationLeaseLost("review run lease lost")
                key = f"run-budget:{task_id}"
                row = session.get(SideEffectOrm, key)
                if row is None:
                    if expected_revision != -1:
                        raise CoordinationLeaseLost("run budget missing")
                    session.add(SideEffectOrm(
                        idempotency_key=key, task_id=task_id, effect_type="review-run-budget-v1",
                        target=task_id, status="active", request_hash=hashlib.sha256(task_id.encode()).hexdigest(),
                        result=payload,
                    ))
                else:
                    changed = session.execute(update(SideEffectOrm).where(
                        SideEffectOrm.idempotency_key == key,
                        SideEffectOrm.result["revision"].as_integer() == expected_revision,
                    ).values(result=payload, status="active"))
                    if changed.rowcount != 1:
                        raise CoordinationLeaseLost("run budget revision changed")
        except IntegrityError as exc:
            raise _runtime_integrity_error(exc, "run budget") from None

    def save_coordination_runtime(self, task_id: str, fingerprint: str, payload: dict,
                                  expected_revision: int, expected_budget: dict | None,
                                  lease: dict | None = None) -> None:
        """运行账本与任务共享预算在一个事务内 CAS；旧 worker 不能预留或发布。"""
        from app.services.coordination_runtime import CoordinationLeaseLost

        try:
            with self._session_factory.begin() as session:
                task = session.get(ReviewTaskOrm, task_id)
                if task is None or task.deleted_at is not None:
                    raise CoordinationLeaseLost("missing coordination task")
                cancelled = task.status == TaskStatus.cancelled.value
                if cancelled:
                    if payload["status"] not in {"interrupted", "cancelled"}:
                        raise CoordinationLeaseLost("coordination task cancelled")
                    payload["status"] = "cancelled"
                    if payload.get("plan"):
                        payload["plan"]["status"] = "cancelled"
                if lease and not cancelled:
                    job = session.get(WorkerJobOrm, lease["job_id"])
                    if (job is None or job.status != "leased" or job.lease_owner != lease["owner"]
                            or job.attempts != lease["attempt"] or job.lease_until is None
                            or job.lease_until.replace(tzinfo=timezone.utc) <= utcnow()):
                        raise CoordinationLeaseLost("coordination worker lease lost")
                key = f"cross-unit:{task_id}:{fingerprint}"
                row = session.get(SideEffectOrm, key)
                if row is None:
                    if expected_revision != -1:
                        raise CoordinationLeaseLost("coordination runtime missing")
                    session.add(SideEffectOrm(idempotency_key=key, task_id=task_id,
                        effect_type="cross-unit-runtime-v1", target=fingerprint, status=payload["status"],
                        request_hash=fingerprint, result=payload))
                else:
                    changed = session.execute(update(SideEffectOrm).where(
                        SideEffectOrm.idempotency_key == key,
                        SideEffectOrm.result["revision"].as_integer() == expected_revision,
                    ).values(result=payload, status=payload["status"]))
                    if changed.rowcount != 1:
                        raise CoordinationLeaseLost("coordination runtime revision changed")
                budget_key = f"cross-unit-budget:{task_id}"
                budget_row = session.get(SideEffectOrm, budget_key)
                new_budget = {"budget": payload["budget"]}
                if budget_row is None:
                    if expected_budget is not None:
                        raise CoordinationLeaseLost("shared budget missing")
                    session.add(SideEffectOrm(idempotency_key=budget_key, task_id=task_id,
                        effect_type="cross-unit-budget-v1", target=task_id, status="active",
                        request_hash=stable_hash(task_id), result=new_budget))
                else:
                    changed = session.execute(update(SideEffectOrm).where(
                        SideEffectOrm.idempotency_key == budget_key,
                        SideEffectOrm.result == {"budget": expected_budget},
                    ).values(result=new_budget))
                    if changed.rowcount != 1:
                        raise CoordinationLeaseLost("shared coordination budget changed")
        except IntegrityError as exc:
            raise _runtime_integrity_error(exc, "coordination runtime") from None

    def begin_side_effect(
        self, *, task_id: str, effect_type: str, target: str, payload: dict[str, Any]
    ) -> tuple[str, bool]:
        key = stable_hash({
            "task_id": task_id,
            "effect_type": effect_type,
            "target": target,
            "payload": payload,
        })
        with self._session_factory.begin() as session:
            existing = session.get(SideEffectOrm, key)
            if existing:
                return key, existing.status == "completed"
            session.add(SideEffectOrm(
                idempotency_key=key,
                task_id=task_id,
                effect_type=effect_type,
                target=target,
                status="started",
                request_hash=stable_hash(payload),
            ))
            return key, False

    def complete_side_effect(self, key: str, result: dict[str, Any]) -> None:
        with self._session_factory.begin() as session:
            row = session.get(SideEffectOrm, key)
            if row is None:
                raise KeyError(key)
            row.status = "completed"
            row.result = result
            row.completed_at = utcnow()

    def _sync_units(self, session: Session, task_row: ReviewTaskOrm, task: ReviewTask) -> None:
        results = {item.review_unit_id: item for item in task.review_unit_results}
        existing = {
            row.unit_id: row for row in session.scalars(
                select(ReviewUnitOrm).where(ReviewUnitOrm.task_id == task.id)
            )
        }
        now = utcnow()
        for unit in task.review_units:
            if unit.grouping_reason == "cross_unit_followup":
                raise ValueError("followup Unit cannot enter normal Unit cache")
            result = results.get(unit.id)
            status = result.status.value if result else ReviewUnitStatus.pending.value
            row = existing.get(unit.id)
            if row is None:
                row = ReviewUnitOrm(
                    task_id=task.id,
                    unit_id=unit.id,
                    unit_snapshot=unit.model_dump(mode="json"),
                    fingerprint=unit.fingerprint,
                    status=status,
                    model=task.model or settings.repoguardian_model,
                    provider=settings.repoguardian_provider,
                    prompt_version=settings.repoguardian_prompt_version,
                    rule_version=settings.repoguardian_rule_version,
                    tool_schema_version=settings.repoguardian_tool_schema_version,
                    planner_version=task_row.planner_version or PLANNER_VERSION,
                    review_policy_version=settings.repoguardian_review_policy_version,
                )
                session.add(row)
            row.unit_snapshot = unit.model_dump(mode="json")
            row.fingerprint = unit.fingerprint
            row.status = status
            row.result_snapshot = result.model_dump(mode="json") if result else None
            row.budget = result.execution_budget.model_dump(mode="json") if result else {}
            row.failure_reason = result.error if result else None
            row.updated_at = now
            if result:
                row.attempts = max(row.attempts or 0, 1)
                row.started_at = row.started_at or now
                if result.status in {
                    ReviewUnitStatus.completed,
                    ReviewUnitStatus.failed,
                    ReviewUnitStatus.timed_out,
                    ReviewUnitStatus.cancelled,
                }:
                    row.finished_at = now

    def _sync_issues(self, session: Session, task_row: ReviewTaskOrm, task: ReviewTask) -> None:
        del task_row
        units = {
            row.unit_id: row.id for row in session.scalars(
                select(ReviewUnitOrm).where(ReviewUnitOrm.task_id == task.id)
            )
        }
        existing = {
            row.issue_id: row for row in session.scalars(
                select(ReviewIssueOrm).where(ReviewIssueOrm.task_id == task.id)
            )
        }
        for issue in task.issues:
            payload = issue.model_dump(mode="json")
            evidence = payload.get("primary_evidence") or {}
            evidence_hash = evidence.get("anchor_hash") or stable_hash(evidence)
            row = existing.get(issue.id)
            if row is None:
                row = ReviewIssueOrm(
                    task_id=task.id,
                    issue_id=issue.id,
                    candidate=payload,
                    final_status=issue.status.value,
                )
                session.add(row)
            row.unit_id = units.get(issue.review_unit_id)
            row.candidate = payload
            row.evidence_resolution = evidence
            row.dedup_source = {
                "review_unit_ids": issue.source_review_unit_ids,
                "issue_ids": issue.source_issue_ids,
            }
            row.evidence_hash = evidence_hash
            row.final_status = issue.status.value
            row.publication_status = "published" if issue.status == IssueStatus.published else "unpublished"
            row.updated_at = utcnow()

    def _sync_patches(self, session: Session, task_row: ReviewTaskOrm, task: ReviewTask) -> None:
        issues = {
            row.issue_id: row for row in session.scalars(
                select(ReviewIssueOrm).where(ReviewIssueOrm.task_id == task.id)
            )
        }
        existing = {
            row.patch_id: row for row in session.scalars(
                select(PatchOrm).where(PatchOrm.task_id == task.id)
            )
        }
        for patch in task.patches:
            payload = patch.model_dump(mode="json")
            diff = patch.diff_content
            evidence_hash = stable_hash(sorted(patch.issue_ids))
            fingerprint, diff_hash = patch_fingerprint(
                head_sha=patch.head_sha,
                issue_evidence_hash=evidence_hash,
                unified_diff=diff,
                patch_policy_version=settings.repoguardian_patch_policy_version,
            )
            inline, artifact_uri = self._externalize_text(task.id, "patch", diff)
            row = existing.get(patch.id)
            if row is None:
                row = PatchOrm(
                    task_id=task.id,
                    patch_id=patch.id,
                    diff_hash=diff_hash,
                    fingerprint=fingerprint,
                    head_sha=patch.head_sha,
                    issue_evidence_hash=evidence_hash,
                    status=patch.status.value,
                    patch_snapshot=payload,
                )
                session.add(row)
                session.flush()
            elif row.diff_hash != diff_hash:
                row.revision += 1
            row.unified_diff = inline
            row.diff_artifact_uri = artifact_uri
            row.diff_hash = diff_hash
            row.fingerprint = fingerprint
            row.head_sha = patch.head_sha
            row.issue_evidence_hash = evidence_hash
            row.status = patch.status.value
            row.policy_result = _model_dump(getattr(patch, "apply_check", None))
            row.patch_snapshot = payload
            row.updated_at = utcnow()
            self._record_artifact(session, task.id, "patch", artifact_uri)
            session.flush()
            session.execute(delete(PatchIssueLinkOrm).where(PatchIssueLinkOrm.patch_id == row.id))
            for issue_id in patch.issue_ids:
                issue = issues.get(issue_id)
                if issue:
                    session.add(PatchIssueLinkOrm(patch_id=row.id, issue_id=issue.id))

    def _sync_validations(self, session: Session, task_row: ReviewTaskOrm, task: ReviewTask) -> None:
        del task_row
        patches = {
            row.patch_id: row for row in session.scalars(
                select(PatchOrm).where(PatchOrm.task_id == task.id)
            )
        }
        existing = {
            row.validation_id: row for row in session.scalars(
                select(ValidationOrm).where(ValidationOrm.task_id == task.id)
            )
        }
        for validation in task.validation:
            payload = validation.model_dump(mode="json")
            patch = patches.get(validation.patch_id)
            if patch is None or validation.patch_sha != patch.diff_hash:
                continue
            environment = str(payload.get("environment_fingerprint") or "unknown")
            fingerprint = validation_fingerprint(
                patch_hash=patch.diff_hash,
                backend=validation.backend,
                validation_profile=str(payload.get("profile") or task.validation_profile),
                environment_fingerprint=environment,
            )
            row = existing.get(validation.id)
            if row is None:
                row = ValidationOrm(
                    task_id=task.id,
                    patch_id=patch.id,
                    validation_id=validation.id,
                    backend=validation.backend,
                    environment_fingerprint=environment,
                    patch_hash=patch.diff_hash,
                    validation_profile=str(payload.get("profile") or task.validation_profile),
                    fingerprint=fingerprint,
                    status=validation.status.value,
                )
                session.add(row)
            row.request = {"profile": row.validation_profile, "patch_id": patch.patch_id}
            row.result = payload
            row.trust_source = validation.trust_source
            row.status = validation.status.value
            row.completed_at = utcnow()
            row.updated_at = utcnow()

    def _serialize_task_snapshot(self, task: ReviewTask) -> tuple[dict[str, Any], str | None]:
        payload = task.model_dump(mode="json")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) <= settings.repoguardian_artifact_inline_max_bytes:
            return payload, None
        artifact = self._artifacts.put_text(task_id=task.id, kind="task-snapshot", content=encoded)
        return {
            "id": task.id,
            "status": task.status.value,
            "phase": task.phase.value,
            "pr_url": task.pr_url,
            "artifact_uri": artifact.uri,
        }, artifact.uri

    def _load_task_snapshot(self, row: ReviewTaskOrm) -> dict[str, Any]:
        if row.task_snapshot_artifact_uri:
            payload = json.loads(self._artifacts.read_text(row.task_snapshot_artifact_uri))
        else:
            payload = dict(row.task_snapshot)
        payload.update({
            "status": row.status,
            "phase": row.current_phase,
            "model": row.model,
            "warnings": list(row.warnings),
            "error": row.error_summary,
            "report_markdown": row.report_markdown,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        })
        review = dict(payload.get("review") or {})
        review.update({
            "mode": row.mode,
            "status": row.status,
            "completed": row.status in {
                TaskStatus.completed.value, TaskStatus.completed_with_warnings.value
            },
        })
        payload["review"] = review
        if row.status == TaskStatus.waiting_for_human.value:
            # 仅转换读取视图；保留历史快照和审计记录，不伪造完成或人工回答。
            payload.update(status=TaskStatus.failed.value, phase="failed",
                           error="历史审查因人工请求停止；该流程已移除，请新建审查")
            review.update(status=TaskStatus.failed.value, completed=False)
            payload["warnings"].append("历史人工暂停任务未完成后续验证，不能作为完整审查结论")
        if row.status == TaskStatus.cancelled.value:
            if payload.get("coordination_plan"):
                payload["coordination_plan"]["status"] = "cancelled"
            if payload.get("cross_unit_risk"):
                payload["cross_unit_risk"].update(execution_status="cancelled",
                                                  non_execution_reason="用户已取消任务")
        return payload

    def _externalize_text(self, task_id: str, kind: str, content: str) -> tuple[str | None, str | None]:
        if len(content.encode("utf-8")) <= settings.repoguardian_artifact_inline_max_bytes:
            return content, None
        artifact = self._artifacts.put_text(task_id=task_id, kind=kind, content=content)
        return None, artifact.uri

    def _record_artifact(
        self, session: Session, task_id: str, kind: str, uri: str | None
    ) -> None:
        if not uri:
            return
        data = self._artifacts.read_text(uri).encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        existing = session.scalar(select(ArtifactOrm).where(ArtifactOrm.uri == uri))
        if existing is None:
            session.add(ArtifactOrm(
                id=uuid4().hex,
                task_id=task_id,
                kind=kind,
                uri=uri,
                sha256=digest,
                size_bytes=len(data),
                retention_until=utcnow() + timedelta(days=settings.repoguardian_retention_days),
            ))

    @staticmethod
    def _human_detail(row: HumanRequestOrm) -> HumanRequestDetail:
        answer = None
        if row.status == HumanRequestStatus.answered.value:
            answer = HumanRequestAnswer(option_id=row.answer_option_id, text=row.answer_text)
        return HumanRequestDetail(
            request_id=row.request_id,
            task_id=row.task_id,
            reason=row.reason,
            question=row.question,
            options=[HumanRequestOption.model_validate(item) for item in row.options],
            context=row.context,
            status=HumanRequestStatus(row.status),
            answer=answer,
            answered_by=row.answered_by,
            deadline=row.deadline,
            created_at=row.created_at,
            answered_at=row.answered_at,
        )


def _repository_from_pr_url(pr_url: str) -> str | None:
    parts = pr_url.rstrip("/").split("/")
    if len(parts) >= 4 and "pull" in parts:
        pull_index = parts.index("pull")
        if pull_index >= 2:
            return f"{parts[pull_index - 2]}/{parts[pull_index - 1]}"
    return None


def _model_dump(value: Any) -> dict[str, Any] | None:
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
