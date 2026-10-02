"""候选生命周期的可选审计回调；不进入业务状态、数据库或模型输入。"""

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
from typing import Callable, Iterator
from uuid import uuid4

from app.models.review import ReviewIssue


_sink: ContextVar[Callable[[dict], None] | None] = ContextVar("issue_audit_sink", default=None)
_unit_id: ContextVar[str] = ContextVar("issue_audit_unit_id", default="unassigned")


@contextmanager
def issue_audit_unit(unit_id: str) -> Iterator[None]:
    token = _unit_id.set(unit_id)
    try:
        yield
    finally:
        _unit_id.reset(token)


def audit_schema_input(raw: object, *, stage: str = "schema_input", batch_id: str | None = None,
                       candidate_index: int | None = None) -> str:
    identity = "schema-" + uuid4().hex
    sink = _sink.get()
    if sink is not None:
        # 不把未校验的模型文本、任意路径或凭据复制到审计中。
        encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True).encode("utf-8")
        sink({"stage": stage, "issue_id": identity, "review_unit_id": _unit_id.get(),
              "status": "unvalidated", "reason": None, "canonical_id": None,
              "payload_sha256": hashlib.sha256(encoded).hexdigest(),
              "payload_bytes": len(encoded), "payload_type": type(raw).__name__,
              "batch_id": batch_id, "candidate_index": candidate_index})
    return identity


def audit_schema_result(identity: str, *, status: str, reason: str,
                        canonical_id: str | None = None, errors: list[dict] | None = None) -> None:
    sink = _sink.get()
    if sink is not None:
        sink({"stage": "schema_validation", "issue_id": identity,
              "review_unit_id": _unit_id.get(), "status": status, "reason": reason,
              "canonical_id": canonical_id, "errors": errors or []})


@contextmanager
def capture_issue_audit(sink: Callable[[dict], None]) -> Iterator[None]:
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


def audit_issue(stage: str, issue: ReviewIssue, *, reason: str | None = None,
                canonical_id: str | None = None) -> None:
    sink = _sink.get()
    if sink is not None:
        sink({"stage": stage, "issue_id": issue.id, "review_unit_id": issue.review_unit_id,
              "status": issue.status.value, "reason": reason,
              "canonical_id": canonical_id, "issue": issue.model_dump(mode="json")})
