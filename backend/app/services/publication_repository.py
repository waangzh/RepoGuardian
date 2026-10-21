"""SQLite 发布事务与 PR 租约；发送意图必须先于外部请求提交。"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select, update, or_, exists
from sqlalchemy.dialects.sqlite import insert

from app.core.database import sync_session
from app.models.orm import (PublicationAttemptOrm as Attempt, PublicationItemOrm as Item,
    PublicationRequestOrm as RequestKey, PublicationBatchOrm as Batch, GitHubSummarySlotOrm as Slot, WorkerJobOrm, utcnow)
from app.models.publication import PublicationAttempt, PublicationItem, GitHubSummarySlot
from app.services.fingerprints import stable_hash
from app.services.review_task_access import lock_live_review


class PublicationConflict(ValueError):
    pass


class PublicationLeaseLost(PublicationConflict):
    pass


@dataclass(frozen=True)
class PublicationLease:
    slot_id: str
    owner: str
    revision: int
    job: object = None


def finding_fingerprint(finding):
    return stable_hash({"anchor": finding.anchor.model_dump(mode="json"), "body": finding.body_markdown,
                        "severity": finding.severity})


class PublicationRepository:
    def __init__(self, session_factory=sync_session):
        self.sessions = session_factory

    def create(self, plan, selection, context, key):
        selected = selection.model_dump(mode="json")
        selected["finding_keys"] = sorted(set(selected["finding_keys"]))
        plan_hash, selection_hash = stable_hash(plan.model_dump(mode="json")), stable_hash(selected)
        logical = stable_hash([plan.review_id, context["publisher_identity"], plan_hash, selection_hash])
        request_key = stable_hash([context["publisher_identity"], key])
        identity = uuid4().hex
        with self.sessions.begin() as session:
            if not lock_live_review(session, plan.review_id):
                raise PublicationConflict("review_unavailable")
            # 先 INSERT，避免 SQLite read→write 升级竞争；唯一键处理并发重复请求。
            session.execute(insert(Attempt).values(id=identity, review_id=plan.review_id,
                repository_id=context["repository_id"], pr_number=context["pr_number"],
                publisher_identity=context["publisher_identity"], head_sha=plan.snapshot.head_sha,
                plan_hash=plan_hash, selection_hash=selection_hash, logical_key=logical,
                plan=plan.model_dump(mode="json"), selection=selected, context=context).on_conflict_do_nothing(index_elements=["logical_key"]))
            row = session.scalar(select(Attempt).where(Attempt.logical_key == logical))
            session.execute(insert(RequestKey).values(key=request_key, request_hash=logical, publication_id=row.id)
                            .on_conflict_do_nothing(index_elements=["key"]))
            if session.get(RequestKey, request_key).request_hash != logical:
                raise PublicationConflict("idempotency_key_conflict")
            if row.id == identity:
                summary = {"body": plan.summary_markdown}
                session.add(Item(id=uuid4().hex, publication_id=identity, kind="summary", finding_key="",
                    fingerprint=stable_hash(summary), payload_hash=stable_hash(summary), payload=summary))
                for finding in plan.inline_drafts:
                    if finding.finding_key in selected["finding_keys"]:
                        payload = finding.model_dump(mode="json")
                        session.add(Item(id=uuid4().hex, publication_id=identity, kind="inline", finding_key=finding.finding_key,
                            fingerprint=finding_fingerprint(finding), payload_hash=stable_hash(payload), payload=payload))
                session.add(WorkerJobOrm(id=uuid4().hex, task_id=plan.review_id, kind="github_publication", status="queued",
                    idempotency_key="github_publication:" + identity, payload={"publication_id": identity}, max_attempts=5))
        return self.get(row.id)

    def get(self, identity):
        with self.sessions() as session:
            row = session.get(Attempt, identity)
            if row is None:
                return None
            result = PublicationAttempt.model_validate(row)
            result.items = [PublicationItem.model_validate(item) for item in session.scalars(
                select(Item).where(Item.publication_id == identity).order_by(Item.kind, Item.finding_key))]
            return result

    def existing_request(self, review_id, selection, publisher_identity, key):
        selected = selection.model_dump(mode="json")
        selected["finding_keys"] = sorted(set(selected["finding_keys"]))
        logical = stable_hash([review_id, publisher_identity, selection.expected_plan_hash, stable_hash(selected)])
        with self.sessions() as session:
            request = session.get(RequestKey, stable_hash([publisher_identity, key]))
            if request is None:
                return None
            if request.request_hash != logical:
                raise PublicationConflict("idempotency_key_conflict")
            identity = request.publication_id
        return self.get(identity)

    def frozen(self, identity):
        with self.sessions() as session:
            row = session.get(Attempt, identity)
            if row is None:
                raise PublicationConflict("publication_not_found")
            return dict(row.plan), dict(row.selection), dict(row.context)

    def list(self, review_id):
        with self.sessions() as session:
            ids = list(session.scalars(select(Attempt.id).where(Attempt.review_id == review_id).order_by(Attempt.requested_at.desc())))
        return [self.get(identity) for identity in ids]

    def acquire(self, attempt, owner, job=None):
        slot_id = stable_hash([attempt.repository_id, attempt.pr_number, attempt.publisher_identity])
        now = utcnow()
        with self.sessions.begin() as session:
            session.execute(insert(Slot).values(id=slot_id, repository_id=attempt.repository_id,
                pr_number=attempt.pr_number, publisher_identity=attempt.publisher_identity).on_conflict_do_nothing())
            result = session.execute(update(Slot).where(Slot.id == slot_id,
                or_(Slot.lease_until.is_(None), Slot.lease_until < now))
                .values(lease_owner=owner, lease_until=now + timedelta(seconds=300), revision=Slot.revision + 1))
            if result.rowcount != 1:
                raise PublicationConflict("publication_busy")
            revision = session.get(Slot, slot_id).revision
        return PublicationLease(slot_id, owner, revision, job)

    @contextmanager
    def locked(self, lease):
        now = utcnow()
        with self.sessions.begin() as session:
            condition = [Slot.id == lease.slot_id, Slot.lease_owner == lease.owner,
                         Slot.revision == lease.revision, Slot.lease_until > now]
            if lease.job is not None:
                job = lease.job
                condition.append(exists(select(WorkerJobOrm.id).where(WorkerJobOrm.id == job.id,
                    WorkerJobOrm.lease_owner == job.lease_owner, WorkerJobOrm.attempts == job.attempts,
                    WorkerJobOrm.status == "leased", WorkerJobOrm.lease_until > now)))
            result = session.execute(update(Slot).where(*condition).values(lease_until=now + timedelta(seconds=300)))
            if result.rowcount != 1:
                raise PublicationLeaseLost("publication_lease_lost")
            yield session

    def slot(self, lease):
        with self.locked(lease) as session:
            return GitHubSummarySlot.model_validate(session.get(Slot, lease.slot_id))

    def lookup_slot(self, repository_id, pr_number, publisher_identity):
        with self.sessions() as session:
            row = session.get(Slot, stable_hash([repository_id, pr_number, publisher_identity]))
            return GitHubSummarySlot.model_validate(row) if row else None

    def item_payload(self, identity):
        with self.sessions() as session:
            return dict(session.get(Item, identity).payload)

    def summary_intent(self, attempt, item, body, *, lease):
        with self.locked(lease) as session:
            slot = session.get(Slot, lease.slot_id)
            if slot.pending_publication_id not in {None, attempt.id}:
                raise PublicationConflict("summary_operation_uncertain")
            slot.pending_publication_id = attempt.id
            row = session.get(Item, item.id)
            row.payload, row.payload_hash, row.state = {"body": body}, stable_hash(body), "publishing"
            row.last_attempt_at = utcnow()

    def summary_finish(self, attempt, item, comment, body, context, *, lease):
        with self.locked(lease) as session:
            slot = session.get(Slot, lease.slot_id)
            if slot.latest_order > context["order"]:
                raise PublicationConflict("summary_newer_review")
            slot.github_comment_id, slot.github_url = comment["id"], comment["html_url"]
            slot.latest_review_id, slot.latest_head_sha = attempt.review_id, attempt.head_sha
            slot.latest_order, slot.last_payload_hash = context["order"], stable_hash(body)
            slot.pending_publication_id = None
            row = session.get(Item, item.id)
            row.state, row.github_comment_id, row.github_url = "published", comment["id"], comment["html_url"]
            row.last_error_code = None

    def summary_rejected(self, attempt, item, code, *, lease):
        with self.locked(lease) as session:
            slot = session.get(Slot, lease.slot_id)
            if slot.pending_publication_id == attempt.id:
                slot.pending_publication_id = None
            row = session.get(Item, item.id)
            row.state, row.last_error_code = "failed", code

    def release(self, lease):
        with self.sessions.begin() as session:
            session.execute(update(Slot).where(Slot.id == lease.slot_id, Slot.lease_owner == lease.owner,
                Slot.revision == lease.revision).values(lease_owner=None, lease_until=None))

    def set_state(self, identity, state, error=None, *, lease):
        with self.locked(lease) as session:
            row = session.get(Attempt, identity)
            row.state, row.last_error_code = state, error
            row.completed_at = None if state in {"queued", "preparing", "publishing"} else utcnow()

    def save_item(self, identity, *, lease, **values):
        with self.locked(lease) as session:
            row = session.get(Item, identity)
            for name, value in values.items():
                if name not in {"state", "payload", "payload_hash", "github_comment_id", "github_review_id", "github_url", "last_error_code"}:
                    raise ValueError("invalid_publication_item_field")
                setattr(row, name, value)
            row.last_attempt_at = utcnow()

    def retry(self, identity):
        with self.sessions.begin() as session:
            review_id = select(Attempt.review_id).where(Attempt.id == identity).scalar_subquery()
            if not lock_live_review(session, review_id):
                raise PublicationConflict("review_unavailable")
            job = session.scalar(select(WorkerJobOrm).where(WorkerJobOrm.idempotency_key == "github_publication:" + identity))
            if job is None or (job.status == "leased" and job.lease_until and job.lease_until.replace(tzinfo=utcnow().tzinfo) > utcnow()):
                raise PublicationConflict("publication_busy")
            row = session.get(Attempt, identity)
            if row.state not in {"failed", "partial", "uncertain"}:
                raise PublicationConflict("publication_not_retryable")
            job.status, job.available_at, job.max_attempts = "retry", utcnow(), job.attempts + 5
            job.lease_owner, job.lease_until = None, None
            row.state, row.completed_at = "queued", None
        return self.get(identity)

    def batches(self, identity):
        with self.sessions() as session:
            return [{"id": row.id, "item_ids": list(row.item_ids), "payload": dict(row.payload),
                     "state": row.state, "github_review_id": row.github_review_id}
                    for row in session.scalars(select(Batch).where(Batch.publication_id == identity).order_by(Batch.id))]

    def prepare_batch(self, identity, publication_id, item_ids, payload, *, lease):
        with self.locked(lease) as session:
            slot = session.get(Slot, lease.slot_id)
            if slot.pending_publication_id not in {None, publication_id}:
                raise PublicationConflict("summary_operation_uncertain")
            slot.pending_publication_id = publication_id
            row = session.get(Batch, identity)
            if row is not None and row.state != "failed":
                raise PublicationConflict("batch_already_sent")
            if row is None:
                row = Batch(id=identity, publication_id=publication_id, item_ids=item_ids, payload=payload)
                session.add(row)
            row.state, row.last_error_code = "publishing", None
            for item_id in item_ids:
                item = session.get(Item, item_id)
                item.state, item.last_attempt_at = "publishing", utcnow()

    def finish_batch(self, identity, receipts, state, error=None, *, lease):
        with self.locked(lease) as session:
            row = session.get(Batch, identity)
            row.state, row.last_error_code = state, error
            slot = session.get(Slot, lease.slot_id)
            if state in {"published", "failed"} and slot.pending_publication_id == row.publication_id:
                slot.pending_publication_id = None
            for item_id in row.item_ids:
                item = session.get(Item, item_id)
                if item_id in receipts:
                    remote = receipts[item_id]
                    item.state, item.github_comment_id, item.github_review_id = "published", remote["id"], remote["pull_request_review_id"]
                    item.github_url, item.last_error_code = remote["html_url"], None
                    row.github_review_id = item.github_review_id
                elif item.state != "published":
                    item.state, item.last_error_code = ("failed" if state == "failed" else "uncertain"), error
