"""复用 side_effects JSON 工件，无新表；独立预算不写主审查账本。"""

from datetime import timezone
from uuid import uuid4

from sqlalchemy import exists, select, update

from app.models.orm import SideEffectOrm, ReviewTaskOrm, WorkerJobOrm, PublicationAttemptOrm, utcnow
from app.services.review.review_task_access import lock_live_review


def synthesis_key(review_id):
    return "review-synthesis-v1:" + review_id


class SynthesisStore:
    def __init__(self, repository, review_id, *, lease=None):
        self.sessions = repository._session_factory
        self.review_id = review_id
        self.lease = lease
        self.key = synthesis_key(review_id)
        self.owner = uuid4().hex

    def _guard(self, session):
        if not lock_live_review(session, self.review_id):
            raise RuntimeError("synthesis_review_unavailable")
        task = session.get(ReviewTaskOrm, self.review_id)
        if task.status not in {"completed", "completed_with_warnings"}:
            raise RuntimeError("synthesis_review_not_terminal")
        if self.lease:
            job = session.get(WorkerJobOrm, self.lease["job_id"])
            if (job is None or job.task_id != self.review_id or job.status != "leased"
                    or job.lease_owner != self.lease["owner"] or job.attempts != self.lease["attempt"]
                    or job.lease_until is None or job.lease_until.replace(tzinfo=timezone.utc) <= utcnow()):
                raise RuntimeError("synthesis_worker_lease_lost")

    def claim(self, artifact):
        with self.sessions.begin() as session:
            self._guard(session)
            row = session.get(SideEffectOrm, self.key)
            if row is not None:
                return False, (row.result or {}).get("artifact")
            result = {"artifact": artifact.model_dump(mode="json"), "budget": None,
                      "revision": -1, "owner": self.owner}
            session.add(SideEffectOrm(idempotency_key=self.key, task_id=self.review_id,
                effect_type="review-synthesis-v1", target=self.review_id, status="started",
                request_hash=artifact.fact_bundle_hash, result=result))
            return True, result["artifact"]

    def load(self, run_id):
        with self.sessions() as session:
            row = session.get(SideEffectOrm, self.key)
            return (row.result or {}).get("budget") if row else None

    def save(self, run_id, payload, revision):
        with self.sessions.begin() as session:
            self._guard(session)
            row = session.get(SideEffectOrm, self.key)
            if (row is None or row.status != "started" or row.result["owner"] != self.owner
                    or row.result["revision"] != revision - 1):
                raise RuntimeError("synthesis_budget_revision_conflict")
            result = {**row.result, "budget": payload, "revision": revision}
            changed = session.execute(update(SideEffectOrm).where(
                SideEffectOrm.idempotency_key == self.key,
                SideEffectOrm.result["revision"].as_integer() == revision - 1,
            ).values(result=result))
            if changed.rowcount != 1:
                raise RuntimeError("synthesis_budget_revision_conflict")

    def finish(self, artifact):
        with self.sessions.begin() as session:
            self._guard(session)
            row = session.get(SideEffectOrm, self.key)
            if row is None or row.status != "started" or row.result["owner"] != self.owner:
                raise RuntimeError("synthesis_artifact_frozen")
            # 发布请求已冻结时不替换正文。预检无写入，正文变化会由 Plan Hash 要求重新预检。
            if session.scalar(select(exists().where(PublicationAttemptOrm.review_id == self.review_id))):
                artifact = artifact.model_copy(update={"status": "fallback", "synthesis": None,
                    "failure_code": "publication_frozen"})
            row.result = {**row.result, "artifact": artifact.model_dump(mode="json")}
            row.status = "completed"
            row.completed_at = utcnow()
            return artifact


def load_synthesis(session, review_id):
    row = session.get(SideEffectOrm, synthesis_key(review_id))
    if row is None:
        return None
    result = row.result or {}
    artifact = result.get("artifact")
    if not isinstance(artifact, dict):
        return None
    from app.models.review_synthesis import ReviewSynthesisArtifact
    try:
        ReviewSynthesisArtifact.model_validate(artifact)
    except ValueError:
        return None
    # 未完成发送的预算保留原始 dispatching/unknown 信息，不将其伪装为零成本。
    return {**artifact, "budget": result.get("budget") or {}}
