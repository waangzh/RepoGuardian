from copy import deepcopy

import pytest

from app.services.review.review_findings import finding_views
from tests.support.review_tasks import publishable_task as publishable_task


def test_public_finding_is_self_contained_and_disposition_is_independent():
    task = publishable_task(side="base")
    before = deepcopy(task.task_runtime_report)
    item = finding_views(task, {"key-a": {"current": {"status": "ignored", "reason": "人工决定"}}})[0]
    assert item.title == "空返回值破坏调用方契约"
    assert item.message and item.impact and item.recommendation and item.category == "correctness"
    assert item.location.model_dump() == {"path": "src/a.py", "start_line": 7, "end_line": 7, "side": "left"}
    assert item.review_status == "confirmed" and item.disposition.status == "ignored"
    assert item.verification.evidence_status == "verified"
    assert task.task_runtime_report == before


@pytest.mark.parametrize("state,expected", [("candidate", "candidate"), ("needs_human", "unresolved"),
    ("published", "confirmed"), ("dismissed", "dismissed"), ("future", "unknown")])
def test_historical_missing_issue_and_unknown_states(state, expected):
    task = publishable_task()
    record = task.task_runtime_report["findings"][0]
    record.update(status=state, issue=None)
    item = finding_views(task, {})[0]
    assert item.title == record["candidate"]["claim"] and item.location is None
    assert item.review_status == expected and item.evidence_status != "verified"


@pytest.mark.parametrize("changes", [{"resolution_status": "ambiguous"}, {"resolved_side": None}])
def test_unknown_location_is_not_invented(changes):
    task = publishable_task()
    issue = task.task_runtime_report["findings"][0]["issue"]
    issue["primary_evidence"].update(changes)
    issue["resolved_location"] = None
    assert finding_views(task, {})[0].location is None


def test_conflicting_location_does_not_become_a_public_anchor():
    task = publishable_task()
    task.task_runtime_report["findings"][0]["issue"]["resolved_location"]["side"] = "base"
    item = finding_views(task, {})[0]
    assert item.location is None and item.verification.evidence_status == "unknown"
