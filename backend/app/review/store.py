"""Short SQLite transactions own attempts, lease fences and request admission."""

from datetime import timedelta, timezone
from uuid import uuid4

from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.sqlite import insert

from app.core.config import settings
from app.core.database import sync_session
from app.models.orm import (
    FindingDispositionOrm, GitHubSummarySlotOrm, IncrementalWebhookDeliveryOrm,
    PublicationAttemptOrm, PublicationItemOrm, ReviewCandidateOrm, ReviewCreateIntentOrm, ReviewRequestOrm,
    ReviewTaskOrm, ReviewWorkItemOrm, WorkerJobOrm,
)
from app.review.domain import Candidate, Run, TERMINAL, WorkItem, utcnow
from app.services.review.review_task_access import lock_live_review

OVERVIEW_INPUT = 6000
# Maximum verifier initial packet; smaller contexts and remaining budgets lower it.
VERIFICATION_INPUT = 20000
MAX_CANDIDATES = 100


def verification_input_limit(limits, committed_input_tokens=0):
    if limits.total_input_tokens is None:
        from app.review.planner import unit_capacity
        return max(1, min(unit_capacity(limits), limits.context_tokens - limits.output_tokens - 1024))
    available = [VERIFICATION_INPUT, limits.context_tokens - limits.output_tokens]
    if limits.total_input_tokens is not None:
        available.append(limits.total_input_tokens - committed_input_tokens - OVERVIEW_INPUT)
    return max(1, min(available))


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
                head_sha=run.snapshot.head_sha, status="queued", current_phase="prepare",
                model=run.configuration.model, provider=run.configuration.provider,
                task_snapshot=run.model_dump(mode="json"),
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
            if caller_id == "github-webhook":
                recent = list(session.scalars(select(ReviewTaskOrm.id).join(ReviewCreateIntentOrm,
                    ReviewCreateIntentOrm.run_id == ReviewTaskOrm.id).where(
                    ReviewTaskOrm.repository == run.snapshot.repository, ReviewTaskOrm.pr_number == run.pr.number,
                    ReviewCreateIntentOrm.caller_id == caller_id,
                    ReviewTaskOrm.created_at > utcnow() - timedelta(hours=1))))
                if len(recent) >= settings.repoguardian_incremental_review_max_runs_per_hour:
                    raise ValueError("review_frequency_limited")
            session.add(ReviewCreateIntentOrm(id=uuid4().hex, caller_id=caller_id, key=key,
                                             request_hash=request_hash, run_id=run.id))
            for unit in (run.plan.units if run.plan.grouping_status != "pending" else []):
                work = WorkItem(id=uuid4().hex, kind="review", unit_id=unit.id)
                if unit.reused_from:
                    previous_id, work_id = unit.reused_from.split(":")
                    previous = session.get(ReviewWorkItemOrm, work_id)
                    if previous is None or previous.run_id != previous_id:
                        raise ValueError("reuse_origin_missing")
                    before = WorkItem.model_validate(previous.payload)
                    if before.status != "completed" or before.material_delivery != "complete":
                        raise ValueError("reuse_origin_incomplete")
                    work.status, work.material_delivery = "completed", "complete"
                    work.delivered_hunk_ids = list(unit.required_hunk_ids)
                    work.delivered_materials = dict(before.delivered_materials)
                    work.reused_from, work.result = unit.reused_from, before.result
                session.add(ReviewWorkItemOrm(id=work.id, run_id=run.id, kind="review",
                                             subject=unit.id, payload=work.model_dump(mode="json")))
            self._supersede(session, run.snapshot.repository, run.snapshot.repository_id,
                            run.pr.number, run.snapshot.head_sha, run.created_at)
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
                   "updated_at": as_utc(row.updated_at), "error_code": row.error_summary,
                   "completed_at": as_utc(row.completed_at) if row.completed_at else None}
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
            run_row = self._guard(session, run_id, lease)
            row = session.get(ReviewWorkItemOrm, work_id)
            work = WorkItem.model_validate(row.payload)
            if row.run_id != run_id or work.status != "queued":
                return None
            work.status, work.attempt_id = "running", uuid4().hex
            work.attempt += 1
            work.delivered_hunk_ids, work.missing_material_reasons = [], {}
            work.delivered_materials = {}
            work.visible_source_ids = []
            work.material_delivery, work.error_code = "not_started", None
            row.payload = work.model_dump(mode="json")
            run_row.current_phase, run_row.updated_at = work.kind, utcnow()
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
            if utcnow() >= self._run(run_row).hard_deadline:
                raise BudgetExceeded("hard_deadline")
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
            run_row = self._guard(session, run_id, lease)
            if status == "completed" and utcnow() >= self._run(run_row).hard_deadline:
                status, result, error, candidate = "interrupted", None, "hard_deadline", None
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

    def _admit_request(self, session, run, work, estimate, overview, *, kind_override=None):
        now, limits = utcnow(), run.configuration.options.limits
        if now >= run.hard_deadline:
            raise BudgetExceeded("hard_deadline")
        kind = kind_override or ("overview" if overview else work.kind)
        if kind in {"review", "grouping"} and now >= run.review_cutoff:
            raise BudgetExceeded("review_cutoff")
        calls, usage = self._usage(session, run.id)
        if estimate is None:
            estimate = verification_input_limit(limits, usage["committed_input_tokens"])
        protected_calls, protected_tokens = (0, 0) if overview else (1, OVERVIEW_INPUT)
        if kind == "review":
            checks = session.scalars(select(ReviewWorkItemOrm).where(
                ReviewWorkItemOrm.run_id == run.id, ReviewWorkItemOrm.kind == "verification"))
            requested = {(call.work_item_id, call.attempt_id) for call in calls if call.kind == "verification"}
            pending = sum(item.payload["status"] == "queued" or (
                item.payload["status"] == "running"
                and (item.id, item.payload["attempt_id"]) not in requested) for item in checks)
            protected_calls += pending
            protected_tokens += pending * verification_input_limit(limits)
        if work is not None and sum(call.attempt_id == work.attempt_id for call in calls) >= limits.model_calls:
            raise BudgetExceeded("agent_model_limit")
        if (limits.total_model_calls is not None
                and len(calls) + 1 + protected_calls > limits.total_model_calls):
            raise BudgetExceeded("model_call_limit")
        if (limits.total_input_tokens is not None
                and usage["committed_input_tokens"] + estimate + protected_tokens > limits.total_input_tokens):
            raise BudgetExceeded("input_token_limit")
        if estimate + limits.output_tokens > limits.context_tokens:
            raise BudgetExceeded("context_capacity")
        if (limits.total_output_tokens is not None and usage["committed_output_tokens"]
                + (1 + protected_calls) * limits.output_tokens > limits.total_output_tokens):
            raise BudgetExceeded("output_token_limit")
        return {
            "input_tokens": (limits.total_input_tokens - usage["committed_input_tokens"] - protected_tokens
                             if limits.total_input_tokens is not None else None),
            "output_tokens": (limits.total_output_tokens - usage["committed_output_tokens"]
                              - protected_calls * limits.output_tokens
                              if limits.total_output_tokens is not None else None),
            "model_calls": (limits.total_model_calls - len(calls) - protected_calls
                            if limits.total_model_calls is not None else None),
            "agent_model_calls": limits.model_calls - sum(
                call.attempt_id == work.attempt_id for call in calls) if work else None,
        }

    def can_start(self, run_id, work):
        with self.sessions() as session:
            row = session.get(ReviewTaskOrm, run_id)
            run = self._run(row)
            unit = next(unit for unit in run.plan.units if unit.id == work.unit_id)
            from app.review.planner import unit_capacity
            estimate = (min(unit.estimated_input_tokens * 2 + 4096,
                            unit_capacity(run.configuration.options.limits))
                        if work.kind == "review" else None)
            try:
                self._admit_request(session, run, work, estimate, False)
            except BudgetExceeded as exc:
                return str(exc)
            return None

    def reserve(self, run_id, work, estimate, lease, *, overview=False, with_remaining=False):
        with self.sessions.begin() as session:
            row = self._guard(session, run_id, lease)
            run = self._run(row)
            limits = run.configuration.options.limits
            kind = "overview" if overview else work.kind
            if not overview:
                self._attempt(session, run_id, work.id, work.attempt_id)
            remaining = self._admit_request(session, run, work, estimate, overview)
            ticket = uuid4().hex
            session.add(ReviewRequestOrm(id=ticket, run_id=run_id,
                work_item_id=work.id if work else None, attempt_id=work.attempt_id if work else None,
                kind=kind, reserved_input_tokens=estimate, reserved_output_tokens=limits.output_tokens,
                state="reserved", lease_fence=lease))
            return (ticket, remaining) if with_remaining else ticket

    def reserve_grouping(self, run_id, estimate, lease):
        """One durable attempt; a crashed grouping request is never silently replayed."""
        with self.sessions.begin() as session:
            row = self._guard(session, run_id, lease)
            run = self._run(row)
            if run.plan.grouping_status != "pending":
                return None
            if session.scalar(select(ReviewRequestOrm.id).where(
                    ReviewRequestOrm.run_id == run_id, ReviewRequestOrm.kind == "grouping")):
                return None
            self._admit_request(session, run, None, estimate, False, kind_override="grouping")
            ticket = uuid4().hex
            session.add(ReviewRequestOrm(id=ticket, run_id=run_id, kind="grouping",
                reserved_input_tokens=estimate,
                reserved_output_tokens=run.configuration.options.limits.output_tokens,
                state="reserved", lease_fence=lease))
            run.phase = "grouping"
            run.status = "running"
            run.started_at = run.started_at or utcnow()
            self._save(row, run)
            return ticket

    def freeze_grouping(self, run_id, plan, lease):
        """Freeze membership and create its work items in the same fenced transaction."""
        from collections import Counter

        with self.sessions.begin() as session:
            row = self._guard(session, run_id, lease)
            run = self._run(row)
            if run.plan.grouping_status != "pending":
                return run
            if plan.grouping_status not in {"model", "fallback"}:
                raise ValueError("grouping_not_final")
            if utcnow() >= run.hard_deadline:
                raise BudgetExceeded("hard_deadline")
            from app.review.planner import members
            member_map = members(run.changed_files)
            expected = Counter(key for unit in run.plan.units for key in unit.required_hunk_ids)
            actual = Counter(key for unit in plan.units for key in unit.required_hunk_ids)
            if expected != actual or plan.excluded != run.plan.excluded:
                raise ValueError("grouping_scope_changed")
            paths = [path for unit in plan.units for path in unit.paths]
            if len(paths) != len(set(paths)) or len({unit.id for unit in plan.units}) != len(plan.units):
                raise ValueError("grouping_duplicate_identity")
            for unit in plan.units:
                if (not unit.required_hunk_ids or set(unit.paths) != {
                        member_map[key]["path"] for key in unit.required_hunk_ids}
                        or unit.reused_from is not None or len(unit.paths) > 10):
                    raise ValueError("grouping_invalid_membership")
            if session.scalar(select(ReviewWorkItemOrm.id).where(ReviewWorkItemOrm.run_id == run_id)):
                raise ValueError("grouping_already_dispatched")
            for unit in plan.units:
                work = WorkItem(id=uuid4().hex, kind="review", unit_id=unit.id)
                session.add(ReviewWorkItemOrm(id=work.id, run_id=run_id, kind="review",
                    subject=unit.id, payload=work.model_dump(mode="json")))
            run.plan, run.phase = plan, "review"
            self._save(row, run)
            return run

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
                WorkerJobOrm.kind.in_(["review", "overview"]), WorkerJobOrm.status.in_(["queued", "retry", "leased"]))
                .values(status="cancelled", lease_owner=None, lease_until=None))
            return True

    def previous(self, snapshot):
        with self.sessions() as session:
            rows = session.scalars(select(ReviewTaskOrm).where(
                ReviewTaskOrm.repository == snapshot.repository, ReviewTaskOrm.pr_number == snapshot.pr_number,
                ReviewTaskOrm.status.in_(["completed", "partial"]), ReviewTaskOrm.deleted_at.is_(None))
                .order_by(ReviewTaskOrm.created_at.desc()).limit(20))
            for row in rows:
                run = self._run(row)
                if run.snapshot.repository_id == snapshot.repository_id and run.report:
                    return run
        return None

    def _supersede(self, session, repository, repository_id, pr_number, head_sha, observed_at):
        stopped = []
        for row in session.scalars(select(ReviewTaskOrm).where(
            ReviewTaskOrm.repository == repository, ReviewTaskOrm.pr_number == pr_number,
            ReviewTaskOrm.head_sha != head_sha, ReviewTaskOrm.status.in_(["queued", "running"]),
            ReviewTaskOrm.created_at <= observed_at, ReviewTaskOrm.deleted_at.is_(None))):
            old = self._run(row)
            if old.snapshot.repository_id != repository_id:
                continue
            old.status, old.phase, old.error_code = "cancelled", "cancelled", "review_superseded"
            old.completed_at = utcnow()
            self._save(row, old)
            session.execute(update(WorkerJobOrm).where(WorkerJobOrm.task_id == row.id,
                WorkerJobOrm.kind.in_(["review", "overview"]),
                WorkerJobOrm.status.in_(["queued", "retry", "leased"])).values(
                    status="cancelled", lease_owner=None, lease_until=None))
            stopped.append(row.id)
        return stopped

    def supersede(self, repository, repository_id, pr_number, head_sha, observed_at):
        with self.sessions.begin() as session:
            session.execute(update(ReviewTaskOrm).where(ReviewTaskOrm.repository == repository,
                ReviewTaskOrm.pr_number == pr_number).values(updated_at=ReviewTaskOrm.updated_at))
            return self._supersede(session, repository, repository_id, pr_number, head_sha, observed_at)

    def history(self, previous):
        if previous is None or previous.report is None:
            return []
        with self.sessions() as session:
            receipts = {}
            for item in session.scalars(select(PublicationItemOrm).join(PublicationAttemptOrm,
                PublicationAttemptOrm.id == PublicationItemOrm.publication_id).where(
                PublicationAttemptOrm.review_id == previous.id,
                PublicationItemOrm.kind == "inline", PublicationItemOrm.state == "published")):
                receipts[item.finding_key] = {
                    "comment_id": item.github_comment_id, "url": item.github_url,
                    "publication_id": item.publication_id, "payload": item.payload}
            return [{"candidate_id": finding.candidate_id, "finding_id": finding.id,
                "claim": finding.claim, "trigger": finding.trigger, "impact": finding.impact,
                "head_sha": previous.snapshot.head_sha, "run_id": previous.id,
                "sources": [source.model_dump() for source in finding.sources],
                "needs_verification": True, "receipt": receipts.get(finding.id)}
                for finding in previous.report.findings[:MAX_CANDIDATES]]

    def seed_history(self, run_id, candidate, lease):
        with self.sessions.begin() as session:
            row = self._guard(session, run_id, lease)
            run = self._run(row)
            if utcnow() >= run.hard_deadline:
                raise BudgetExceeded("hard_deadline")
            if not any(item["candidate_id"] == candidate.previous_candidate_id for item in run.plan.history):
                raise ValueError("history_candidate_not_in_plan")
            if not candidate.sources or any(source.review_id != run_id or source.commit_sha != (
                run.snapshot.head_sha if source.side == "head" else run.snapshot.base_sha)
                for source in candidate.sources):
                raise ValueError("history_source_mismatch")
            if session.get(ReviewCandidateOrm, candidate.id):
                return
            if len(list(session.scalars(select(ReviewCandidateOrm.id).where(
                ReviewCandidateOrm.run_id == run_id)))) >= MAX_CANDIDATES:
                raise BudgetExceeded("candidate_limit")
            session.add(ReviewCandidateOrm(id=candidate.id, run_id=run_id, attempt_id=candidate.attempt_id,
                tool_call_id=candidate.tool_call_id, payload=candidate.model_dump(mode="json")))
            check = WorkItem(id=uuid4().hex, kind="verification", unit_id=candidate.unit_id,
                             candidate_id=candidate.id)
            session.add(ReviewWorkItemOrm(id=check.id, run_id=run_id, kind="verification",
                subject=candidate.id, payload=check.model_dump(mode="json")))

    def webhook_count(self, repository, pr_number):
        with self.sessions() as session:
            return len(list(session.scalars(select(ReviewTaskOrm.id).join(
                ReviewCreateIntentOrm, ReviewCreateIntentOrm.run_id == ReviewTaskOrm.id).where(
                ReviewTaskOrm.repository == repository, ReviewTaskOrm.pr_number == pr_number,
                ReviewCreateIntentOrm.caller_id == "github-webhook",
                ReviewTaskOrm.created_at > utcnow() - timedelta(hours=1)))))

    def reap_exhausted(self, session, run_ids):
        from app.review.report import build_report
        for run_id in set(run_ids):
            row = session.get(ReviewTaskOrm, run_id)
            if row is None or row.deleted_at or row.status not in {"queued", "running"}:
                continue
            run, works = self._run(row), []
            for item in session.scalars(select(ReviewWorkItemOrm).where(ReviewWorkItemOrm.run_id == run_id)):
                work = WorkItem.model_validate(item.payload)
                if work.status in {"queued", "running"}:
                    work.status, work.error_code = "interrupted", "worker_attempts_exhausted"
                    item.payload = work.model_dump(mode="json")
                works.append(work)
            candidates = [Candidate.model_validate(item.payload) for item in session.scalars(
                select(ReviewCandidateOrm).where(ReviewCandidateOrm.run_id == run_id))]
            report = build_report(run, works, candidates, self._usage(session, run_id)[1])
            if not candidates and not any(work.status == "completed" for work in works):
                report.status = "failed"
            report.reasons.append("worker_attempts_exhausted")
            report.overview.status, report.overview.error_code = "failed", "interrupted_unknown"
            from app.review.planner import identity
            report.version = identity(report.model_dump())
            run.report, run.status, run.phase = report, report.status, "finished"
            run.completed_at, run.error_code = utcnow(), "worker_attempts_exhausted"
            self._save(row, run)

    def list_runs(self, *, status=None, page=1, page_size=20, q=None, repository=None,
                  pr_number=None, head_sha=None):
        filters = [ReviewTaskOrm.deleted_at.is_(None)]
        for column, value in ((ReviewTaskOrm.status, status), (ReviewTaskOrm.repository, repository),
                              (ReviewTaskOrm.pr_number, pr_number), (ReviewTaskOrm.head_sha, head_sha)):
            if value is not None:
                filters.append(column == value)
        with self.sessions() as session:
            runs = [self._run(row) for row in session.scalars(select(ReviewTaskOrm).where(*filters)
                    .order_by(ReviewTaskOrm.created_at.desc()))]
        if q:
            runs = [run for run in runs if q.casefold() in
                    " ".join((run.id, run.pr.title, run.snapshot.repository, run.pr_url)).casefold()]
        start = (page - 1) * page_size
        return {"items": runs[start:start + page_size], "total": len(runs), "page": page, "page_size": page_size}

    def dispositions(self, run_id):
        with self.sessions() as session:
            return {row.finding_id: row.payload for row in session.scalars(
                select(FindingDispositionOrm).where(FindingDispositionOrm.task_id == run_id))}

    def save_disposition(self, run_id, finding_id, value):
        with self.sessions.begin() as session:
            if not lock_live_review(session, run_id):
                raise KeyError(run_id)
            run = self._run(session.get(ReviewTaskOrm, run_id))
            if run.status not in TERMINAL:
                raise ValueError("review_not_terminal")
            session.merge(FindingDispositionOrm(task_id=run_id, finding_id=finding_id,
                                                payload=value, updated_at=utcnow()))

    def _publication_pending(self, session, run_id):
        return session.scalar(select(PublicationAttemptOrm.id).where(
            PublicationAttemptOrm.review_id == run_id,
            or_(PublicationAttemptOrm.state.in_(["queued", "preparing", "publishing", "uncertain"]),
                PublicationAttemptOrm.id.in_(select(GitHubSummarySlotOrm.pending_publication_id)))))

    def retry(self, run_id, *, overview=False):
        with self.sessions.begin() as session:
            if not lock_live_review(session, run_id):
                raise KeyError(run_id)
            row = session.get(ReviewTaskOrm, run_id)
            run = self._run(row)
            allowed = {"completed", "partial", "failed"} if overview else {"partial", "failed"}
            if run.status not in allowed or utcnow() >= run.hard_deadline:
                raise ValueError("review_retry_unavailable")
            if session.scalar(select(WorkerJobOrm.id).where(WorkerJobOrm.task_id == run_id,
                WorkerJobOrm.status.in_(["queued", "retry", "leased"]))):
                raise ValueError("review_job_active")
            if self._publication_pending(session, run_id):
                raise ValueError("review_publication_pending")
            limits, usage = run.configuration.options.limits, self._usage(session, run_id)[1]
            if ((limits.total_model_calls is not None and usage["model_calls"] >= limits.total_model_calls)
                    or (limits.total_input_tokens is not None
                        and usage["committed_input_tokens"] + OVERVIEW_INPUT > limits.total_input_tokens)):
                raise ValueError("review_budget_exhausted")
            if overview:
                if run.report is None or not run.plan.units:
                    raise ValueError("overview_facts_missing")
                run.report.overview = type(run.report.overview)()
            else:
                for item in session.scalars(select(ReviewWorkItemOrm).where(ReviewWorkItemOrm.run_id == run_id)):
                    work = WorkItem.model_validate(item.payload)
                    if work.status != "completed":
                        work.status, work.error_code = "queued", None
                        item.payload = work.model_dump(mode="json")
            run.status, run.phase, run.completed_at = "queued", "overview" if overview else "review", None
            self._save(row, run)
            from app.services.review.task_queue import DatabaseTaskQueue
            DatabaseTaskQueue(self.sessions).enqueue(task_id=run_id, kind="overview" if overview else "review",
                idempotency_key=f"retry:{run_id}:{uuid4().hex}", session=session)

    def save_discussions(self, run_id, uri, lease):
        with self.sessions.begin() as session:
            row = self._guard(session, run_id, lease)
            run = self._run(row)
            run.discussion_uri = uri
            self._save(row, run)

    def fail_run(self, run_id, code, lease):
        from app.review.report import build_report
        run = self.get(run_id)
        report = build_report(run, self.work_items(run_id), self.candidates(run_id), self.usage(run_id))
        report.status = "partial" if report.candidates else "failed"
        report.reasons.append(code)
        report.overview.status, report.overview.error_code = "failed", code
        self.save_report(run_id, report, lease, finished=True)

    def delete_task(self, run_id, *, require_retention=False):
        with self.sessions.begin() as session:
            if not lock_live_review(session, run_id):
                row = session.get(ReviewTaskOrm, run_id)
                return bool(row and row.deleted_at)
            row = session.get(ReviewTaskOrm, run_id)
            if row.status not in TERMINAL:
                raise ValueError("review_not_terminal")
            if require_retention and as_utc(row.retention_until) > utcnow():
                return False
            if self._publication_pending(session, run_id):
                if require_retention:
                    return False
                raise ValueError("review_publication_pending")
            if session.scalar(select(WorkerJobOrm.id).where(WorkerJobOrm.task_id == run_id,
                WorkerJobOrm.status.in_(["queued", "retry", "leased"]))):
                if require_retention:
                    return False
                raise ValueError("review_job_active")
            row.deleted_at, row.updated_at = utcnow(), utcnow()
            row.task_snapshot = {}
            session.execute(delete(ReviewCandidateOrm).where(ReviewCandidateOrm.run_id == run_id))
            session.execute(delete(ReviewWorkItemOrm).where(ReviewWorkItemOrm.run_id == run_id))
            return True

    def list_expired_task_ids(self):
        with self.sessions() as session:
            return list(session.scalars(select(ReviewTaskOrm.id).where(
                ReviewTaskOrm.deleted_at.is_(None), ReviewTaskOrm.status.in_(TERMINAL),
                ReviewTaskOrm.retention_until <= utcnow()).limit(1000)))

    def deleted_ids(self):
        with self.sessions() as session:
            return list(session.scalars(select(ReviewTaskOrm.id).where(ReviewTaskOrm.deleted_at.is_not(None))))

    def claim_webhook_delivery(self, *, delivery_id, repository, pr_number, head_sha):
        with self.sessions.begin() as session:
            result = session.execute(insert(IncrementalWebhookDeliveryOrm).values(
                delivery_id=delivery_id, repository=repository, pr_number=pr_number,
                head_sha=head_sha, status="accepted").on_conflict_do_nothing(index_elements=["delivery_id"]))
            return result.rowcount == 1

    def finish_webhook_delivery(self, delivery_id, *, status, task_id=None, reason=None):
        with self.sessions.begin() as session:
            row = session.get(IncrementalWebhookDeliveryOrm, delivery_id)
            if row:
                row.status, row.task_id, row.reason, row.updated_at = status, task_id, reason, utcnow()

    def get_webhook_delivery(self, delivery_id):
        with self.sessions() as session:
            row = session.get(IncrementalWebhookDeliveryOrm, delivery_id)
            return {key: getattr(row, key) for key in ("delivery_id", "repository", "pr_number",
                    "head_sha", "status", "task_id", "reason")} if row else None
