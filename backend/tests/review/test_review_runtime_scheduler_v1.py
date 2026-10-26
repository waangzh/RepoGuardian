import pytest

from app.models.review_runtime import ReviewWorkItem, WorkItemKind
from app.services.review.runtime.review_runtime import TaskReviewRuntime


def item(identity):
    return ReviewWorkItem(work_item_id=identity, kind=WorkItemKind.inspect,
                          goal_ids=[f"goal-{identity}"])


def test_task_runtime_has_one_writer_and_stable_ready_order():
    runtime = TaskReviewRuntime([item("b"), item("a")])
    assert [value.work_item_id for value in runtime.ready()] == ["a", "b"]
    claimed = runtime.claim_next()
    assert claimed.work_item_id == "a" and claimed.attempt_count == 1
    with pytest.raises(RuntimeError, match="single writer"):
        runtime.claim_next()
    finished = runtime.finish("a")
    assert finished.state.value == "completed" and finished.revision == 1
    assert runtime.claim_next().work_item_id == "b"


def test_task_runtime_rejects_duplicate_and_wrong_writer_completion():
    runtime = TaskReviewRuntime([item("a")])
    with pytest.raises(ValueError, match="duplicate"):
        runtime.enqueue(item("a"))
    with pytest.raises(RuntimeError, match="not owned"):
        runtime.finish("a")
    runtime.claim_next()
    with pytest.raises(RuntimeError, match="not owned"):
        runtime.finish("other")
