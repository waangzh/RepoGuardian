"""候选生命周期的可选审计回调；不进入业务状态、数据库或模型输入。"""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator

from app.models.review import ReviewIssue


_sink: ContextVar[Callable[[dict], None] | None] = ContextVar("issue_audit_sink", default=None)


@contextmanager
def capture_issue_audit(sink: Callable[[dict], None]) -> Iterator[None]:
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


def audit_issue(stage: str, issue: ReviewIssue, *, reason: str | None = None,
                canonical_id: str | None = None, canonical_review_unit_id: str | None = None) -> None:
    sink = _sink.get()
    if sink is not None:
        sink({"stage": stage, "issue_id": issue.id, "review_unit_id": issue.review_unit_id,
              "status": issue.status.value, "reason": reason,
              "canonical_id": canonical_id, "canonical_review_unit_id": canonical_review_unit_id,
              "issue": issue.model_dump(mode="json")})
