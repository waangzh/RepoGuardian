"""Short SQLite transactions own attempts, lease fences and request admission."""

from datetime import timedelta, timezone
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert

from app.core.config import settings
from app.core.database import sync_session
from app.models.orm import (
    ReviewCandidateOrm, ReviewCreateIntentOrm, ReviewRequestOrm, ReviewTaskOrm,
    ReviewWorkItemOrm, WorkerJobOrm,
)
from app.review.domain import Candidate, Run, TERMINAL, WorkItem, utcnow
from app.services.review.review_task_access import lock_live_review

OVERVIEW_INPUT = 6000
VERIFICATION_INPUT = 6000
MAX_CANDIDATES = 100


class LeaseLost(RuntimeError):
    pass


class ReviewStopped(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


def as_utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class ReviewStore:
    def __init__(self, session_factory=sync_session):
        self.sessions = session_factory

    def _guard(self, session, run_id, lease, *, terminal=False):
        if not lock_live_review(session, run_id):
            raise ReviewStopped("review_deleted")
        row = session.get(ReviewTaskOrm, run_id)
        if row.status == "cancelled" or (row.status in TERMINAL and not terminal):
            raise ReviewStopped("review_stopped")
        job = session.get(WorkerJobOrm, lease["job_id"])
        if (job is None or job.task_id != run_id or job.status != "leased"
                or job.lease_owner != lease["owner"] or job.attempts != lease["attempt"]
                or job.lease_until is None or as_utc(job.lease_until) <= utcnow()):
            raise LeaseLost("review_lease_lost")
        return row

    def _attempt(self, session, run_id, work_id, attempt_id):
        row = session.get(ReviewWorkItemOrm, work_id)
        if (row is None or row.run_id != run_id or row.payload["status"] != "running"
                or row.payload["attempt_id"] != attempt_id):
            raise LeaseLost("review_attempt_replaced")
        return row

    def assert_active(self, run_id, lease):
        with self.sessions.begin() as session:
            self._guard(session, run_id, lease)

    def find_intent(self, caller_id, key, request_hash):
        with self.sessions() as session:
            row = session.scalar(select(ReviewCreateIntentOrm).where(
                ReviewCreateIntentOrm.caller_id == caller_id, ReviewCreateIntentOrm.key == key))
            if row and row.request_hash != request_hash:
                raise ValueError("idempotency_key_conflict")
            return row.run_id if row else None

    def create(self, run, caller_id, key, request_hash):
        with self.sessions.begin() as session:
            # The first write serializes same-key creation, before reading the intent.
            inserted = session.execute(insert(ReviewTaskOrm).values(
                id=run.id, repository=run.snapshot.repository, repo_url=run.pr.clone_url,
                pr_url=run.pr_url, pr_number=run.pr.number, base_sha=run.snapshot.declared_base_sha,
                head_sha=run.snapshot.head_sha, mode="review", status="queued", current_phase="prepare",
                model=run.configuration.model, provider=run.configuration.provider,
                warnings=[], task_snapshot=run.model_dump(mode="json"), thread_id=run.id,
                created_at=run.created_at, updated_at=run.updated_at,
                retention_until=utcnow() + timedelta(days=settings.repoguardian_retention_days)
            ).on_conflict_do_nothing(index_elements=["id"]))
            existing = session.scalar(select(ReviewCreateIntentOrm).where(
                ReviewCreateIntentOrm.caller_id == caller_id, ReviewCreateIntentOrm.key == key))
            if existing:
                if existing.request_hash != request_hash:
                    raise ValueError("idempotency_key_conflict")
                if inserted.rowcount:
                    session.delete(session.get(ReviewTaskOrm, run.id))
                return existing.run_id
            session.add(ReviewCreateIntentOrm(id=uuid4().hex, caller_id=caller_id, key=key,
                                             request_hash=request_hash, run_id=run.id))
            for unit in run.plan.units:
                work = WorkItem(id=uuid4().hex, kind="review", unit_id=unit.id)
                session.add(ReviewWorkItemOrm(id=work.id, run_id=run.id, kind="review",
                                             subject=unit.id, payload=work.model_dump(mode="json")))
            session.flush()
            from app.services.review.task_queue import DatabaseTaskQueue
            DatabaseTaskQueue(self.sessions).enqueue(task_id=run.id, session=session)
            return run.id

    def get(self, run_id):
        with self.sessions() as session:
            row = session.get(ReviewTaskOrm, run_id)
            if row is None or row.deleted_at:
                return None
            return self._run(row)

    def _run(self, row):
        payload = {**row.task_snapshot, "status": row.status, "phase": row.current_phase,
                   "updated_at": as_utc(row.updated_at)}
        return Run.model_validate(payload)

    def _save(self, row, run):
        run.updated_at = utcnow()
        row.task_snapshot = run.model_dump(mode="json")
        row.status, row.current_phase = run.status, run.phase
        row.updated_at, row.completed_at = run.updated_at, run.completed_at
        row.error_summary = run.error_code

    def begin(self, run_id, lease):
        with self.sessions.begin() as session:
            row = self._guard(session, run_id, lease)
            run = self._run(row)
            run.status, run.phase = "running", "review"
            run.started_at = run.started_at or utcnow()
            if run.report and run.report.overview.status == "running":
                run.report.overview.status = "failed"
                run.report.overview.error_code = "interrupted_unknown"
            for item in session.scalars(select(ReviewWorkItemOrm).where(ReviewWorkItemOrm.run_id == run_id)):
                work = WorkItem.model_validate(item.payload)
                if work.status == "running":
                    work.status, work.error_code = "queued", "interrupted"
                    item.payload = work.model_dump(mode="json")
            self._save(row, run)
            return run

    def work_items(self, run_id):
        with self.sessions() as session:
            return [WorkItem.model_validate(row.payload) for row in session.scalars(
                select(ReviewWorkItemOrm).where(ReviewWorkItemOrm.run_id == run_id))]

    def candidates(self, run_id):
        with self.sessions() as session:
            return [Candidate.model_validate(row.payload) for row in session.scalars(
                select(ReviewCandidateOrm).where(ReviewCandidateOrm.run_id == run_id))]

    def claim(self, run_id, work_id, lease):
        with self.sessions.begin() as session:
            self._guard(session, run_id, lease)
            row = session.get(ReviewWorkItemOrm, work_id)
            work = WorkItem.model_validate(row.payload)
            if row.run_id != run_id or work.status != "queued":
                return None
            work.status, work.attempt_id = "running", uuid4().hex
            work.attempt += 1
            work.delivered_hunk_ids, work.missing_material_reasons = [], {}
            work.visible_source_ids = []
            work.material_delivery, work.error_code = "not_started", None
            row.payload = work.model_dump(mode="json")
            return work

    def submit(self, run_id, work, candidate, lease):
        with self.sessions.begin() as session:
            run_row = self._guard(session, run_id, lease)
            work_row = self._attempt(session, run_id, work.id, work.attempt_id)
            if utcnow() >= self._run(run_row).hard_deadline:
                raise ReviewStopped("hard_deadline")
            existing = session.scalar(select(ReviewCandidateOrm).where(
                ReviewCandidateOrm.run_id == run_id,
                ReviewCandidateOrm.attempt_id == work.attempt_id,
                ReviewCandidateOrm.tool_call_id == candidate.tool_call_id))
            if existing:
                return existing.id
            count = len(list(session.scalars(select(ReviewCandidateOrm.id).where(
                ReviewCandidateOrm.run_id == run_id))))
            if count >= MAX_CANDIDATES:
                raise ValueError("candidate_limit")
            if (candidate.attempt_id != work.attempt_id or candidate.unit_id != work.unit_id
                    or work.kind != "review"):
                raise ValueError("candidate_identity_mismatch")
            snapshot = self._run(run_row).snapshot
            if not candidate.sources or any(
                source.id not in work_row.payload["visible_source_ids"]
                or source.review_id != run_id
                or source.commit_sha != (snapshot.head_sha if source.side == "head" else snapshot.base_sha)
                for source in candidate.sources
            ):
                raise ValueError("candidate_source_not_visible")
            session.add(ReviewCandidateOrm(id=candidate.id, run_id=run_id,
                attempt_id=work.attempt_id, tool_call_id=candidate.tool_call_id,
                payload=candidate.model_dump(mode="json")))
            check = WorkItem(id=uuid4().hex, kind="verification", unit_id=work.unit_id,
                             candidate_id=candidate.id)
            session.add(ReviewWorkItemOrm(id=check.id, run_id=run_id, kind="verification",
                                         subject=candidate.id, payload=check.model_dump(mode="json")))
            return candidate.id

    def deliver(self, run_id, work, hunk_ids, failures, lease, *, source_ids=(), digests=None):
        with self.sessions.begin() as session:
            run_row = self._guard(session, run_id, lease)
            row = self._attempt(session, run_id, work.id, work.attempt_id)
            saved = WorkItem.model_validate(row.payload)
            required = set(next(unit for unit in self._run(run_row).plan.units
                                if unit.id == work.unit_id).required_hunk_ids)
            if not set(hunk_ids) <= required:
                raise ValueError("unknown_material_member")
            saved.delivered_hunk_ids = sorted(set(saved.delivered_hunk_ids) | set(hunk_ids))
            saved.visible_source_ids = sorted(set(saved.visible_source_ids) | set(source_ids))
            saved.delivered_materials.update(digests or {})
            saved.missing_material_reasons.update(failures)
            for key in hunk_ids:
                saved.missing_material_reasons.pop(key, None)
            saved.material_delivery = ("complete" if required <= set(saved.delivered_hunk_ids)
                                       else "incomplete")
            row.payload = saved.model_dump(mode="json")

    def finish_work(self, run_id, work, status, result, error, lease, *, candidate=None, tool_calls=0):
        with self.sessions.begin() as session:
            self._guard(session, run_id, lease)
            row = self._attempt(session, run_id, work.id, work.attempt_id)
            saved = WorkItem.model_validate(row.payload)
            saved.status, saved.result, saved.error_code = status, result, error
            saved.tool_calls = tool_calls
            row.payload = saved.model_dump(mode="json")
            if candidate is not None:
                stored = session.get(ReviewCandidateOrm, work.candidate_id)
                if stored is None or stored.run_id != run_id or candidate.id != stored.id:
                    raise ValueError("verification_candidate_mismatch")
                stored.payload = candidate.model_dump(mode="json")

    def skip(self, run_id, work_id, reason, lease):
        with self.sessions.begin() as session:
            self._guard(session, run_id, lease)
            row = session.get(ReviewWorkItemOrm, work_id)
            work = WorkItem.model_validate(row.payload)
            if row.run_id == run_id and work.status in {"queued", "running"}:
                work.status, work.error_code = "skipped", reason
                row.payload = work.model_dump(mode="json")

    def save_report(self, run_id, report, lease, *, finished=False):
        with self.sessions.begin() as session:
            row = self._guard(session, run_id, lease)
            run = self._run(row)
            run.report = report
            run.phase = "finished" if finished else "overview"
            if finished:
                run.status, run.completed_at = report.status, utcnow()
            self._save(row, run)

    def _usage(self, session, run_id):
        calls = list(session.scalars(select(ReviewRequestOrm).where(ReviewRequestOrm.run_id == run_id)))
        known = [call.usage for call in calls if call.usage is not None]
        unknown = len(calls) - len(known)
        return calls, {
            "model_calls": len(calls), "unknown_calls": unknown,
            "input_tokens": sum(item["input_tokens"] for item in known) if not unknown else None,
            "output_tokens": sum(item["output_tokens"] for item in known) if not unknown else None,
            "known_input_tokens": sum(item["input_tokens"] for item in known),
            "known_output_tokens": sum(item["output_tokens"] for item in known),
            "committed_input_tokens": sum(call.usage["input_tokens"] if call.usage is not None
                                         else call.reserved_input_tokens for call in calls),
            "committed_output_tokens": sum(call.usage["output_tokens"] if call.usage is not None
                                          else call.reserved_output_tokens for call in calls),
        }

    def usage(self, run_id):
        with self.sessions() as session:
            return self._usage(session, run_id)[1]

    def reserve(self, run_id, work, estimate, lease, *, overview=False):
        with self.sessions.begin() as session:
            row = self._guard(session, run_id, lease)
            run = self._run(row)
            now, limits = utcnow(), run.configuration.options.limits
            if now >= run.hard_deadline:
                raise BudgetExceeded("hard_deadline")
            kind = "overview" if overview else work.kind
            if not overview:
                self._attempt(session, run_id, work.id, work.attempt_id)
                if kind == "review" and now >= run.review_cutoff:
                    raise BudgetExceeded("review_cutoff")
            calls, usage = self._usage(session, run_id)
            protected_calls, protected_tokens = (0, 0) if overview else (1, OVERVIEW_INPUT)
            if kind == "review":
                checks = session.scalars(select(ReviewWorkItemOrm).where(
                    ReviewWorkItemOrm.run_id == run_id, ReviewWorkItemOrm.kind == "verification"))
                requested = {call.work_item_id for call in calls if call.kind == "verification"}
                pending = sum(item.id not in requested and item.payload["status"] in {"queued", "running"}
                              for item in checks)
                protected_calls += pending
                protected_tokens += pending * VERIFICATION_INPUT
            if work is not None and sum(call.attempt_id == work.attempt_id for call in calls) >= limits.model_calls:
                raise BudgetExceeded("agent_model_limit")
            if len(calls) + 1 + protected_calls > limits.total_model_calls:
                raise BudgetExceeded("model_call_limit")
            if usage["committed_input_tokens"] + estimate + protected_tokens > limits.total_input_tokens:
                raise BudgetExceeded("input_token_limit")
            ticket = uuid4().hex
            session.add(ReviewRequestOrm(id=ticket, run_id=run_id,
                work_item_id=work.id if work else None, attempt_id=work.attempt_id if work else None,
                kind=kind, reserved_input_tokens=estimate, reserved_output_tokens=limits.output_tokens,
                state="reserved", lease_fence=lease))
            return ticket

    def settle(self, run_id, ticket, usage, lease):
        with self.sessions.begin() as session:
            self._guard(session, run_id, lease)
            row = session.get(ReviewRequestOrm, ticket)
            if row is None or row.run_id != run_id or row.lease_fence != lease:
                raise LeaseLost("request_lease_mismatch")
            if row.work_item_id:
                self._attempt(session, run_id, row.work_item_id, row.attempt_id)
            if row.state != "reserved":
                return
            if usage is not None:
                for key in ("input_tokens", "output_tokens"):
                    if type(usage.get(key)) is not int or usage[key] < 0:
                        raise ValueError("invalid_usage")
            row.usage, row.state = usage, "settled" if usage is not None else "unknown"

    def cancel(self, run_id):
        with self.sessions.begin() as session:
            if not lock_live_review(session, run_id):
                return False
            row = session.get(ReviewTaskOrm, run_id)
            if row.status in TERMINAL:
                return False
            run = self._run(row)
            run.status, run.phase, run.completed_at = "cancelled", "cancelled", utcnow()
            self._save(row, run)
            session.execute(update(WorkerJobOrm).where(WorkerJobOrm.task_id == run_id,
                WorkerJobOrm.kind == "review", WorkerJobOrm.status.in_(["queued", "retry", "leased"]))
                .values(status="cancelled", lease_owner=None, lease_until=None))
            return True
