from datetime import datetime, timezone

import pytest

from app.models.review import ReviewTask, ReviewRunManifest, TaskStatus
from app.services.review_outcome import derive_review_outcome


def completed_task(**report_updates):
    task = ReviewTask(id="run", pr_url="https://github.com/o/r/pull/1", engine="task_review_v1", status="completed")
    task.run_manifest = ReviewRunManifest(review_id=task.id, engine=task.engine, coverage_version="task-goals-v1",
        head_sha="a" * 40, base_sha="b" * 40, effective_base_sha="c" * 40, usage_integrity="complete",
        provider="fixture", model="fixture", started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        completed_at=datetime(2026, 1, 1, tzinfo=timezone.utc), duration_ms=0, coverage=task.coverage)
    task.task_runtime_report = {"execution_status": "completed", "termination_reason": "plan_exhausted",
        "scope_complete": True, "initial_scope_coverage": [{"status": "examined"}], "usage_integrity": "complete",
        **report_updates}
    return task


def test_complete_nonempty_scope_is_clean():
    outcome = derive_review_outcome(completed_task())
    assert outcome.kind == "clean" and outcome.coverage == "complete"
    assert outcome.total_goals == outcome.examined_goals == 1


@pytest.mark.parametrize("updates,reason", [
    ({"initial_scope_coverage": []}, "scope_empty"),
    ({"initial_scope_coverage": [{"status": "unsupported"}]}, "scope_incomplete"),
    ({"scope_complete": False}, "scope_incomplete"),
    ({"findings": [{"status": "candidate"}]}, "verification_unresolved"),
    ({"findings": [{"status": "needs_human"}]}, "verification_unresolved"),
    ({"findings": [{"status": "future"}]}, "finding_status_unknown"),
    ({"admitted_investigations": [{"status": "answered", "answer": "insufficient_evidence"}]}, "investigation_incomplete"),
    ({"admitted_investigations": [{"status": "future"}]}, "investigation_incomplete"),
    ({"admitted_investigations": [{"status": "not_executed"}]}, "investigation_incomplete"),
    ({"unscheduled_leads": [{"status": "not_scheduled"}]}, "unscheduled_investigation"),
    ({"unscheduled_leads": [{}]}, "unscheduled_investigation"),
    ({"usage_integrity": "unknown"}, "usage_integrity_unknown"),
    ({"usage_integrity": "inconsistent"}, "usage_integrity_unknown"),
    ({"unknown_attempts": 1}, "usage_integrity_unknown"),
    ({"termination_reason": "deadline"}, "termination_incomplete"),
    ({"execution_status": "running"}, "termination_incomplete"),
])
def test_never_clean_with_gaps(updates, reason):
    outcome = derive_review_outcome(completed_task(**updates))
    assert outcome.kind == "inconclusive" and reason in outcome.reason_codes


@pytest.mark.parametrize("status", [TaskStatus.completed, TaskStatus.completed_with_warnings, TaskStatus.failed, TaskStatus.cancelled])
def test_terminal_precedence_and_confirmation_is_not_publication(status):
    task = completed_task(findings=[{"status": "confirmed"}], scope_complete=False)
    task.status = status
    outcome = derive_review_outcome(task)
    assert outcome.kind == {"failed": "failed", "cancelled": "cancelled"}.get(status, "findings")
    assert outcome.confirmed_count == 1 and outcome.publishable_count == 0
    assert outcome.coverage == "partial"


@pytest.mark.parametrize("problem,reason", [("legacy", "unsupported_legacy_projection"),
    ("missing", "runtime_report_missing"), ("invalid", "runtime_report_invalid"), ("snapshot", "snapshot_missing"),
    ("identity", "snapshot_missing"), ("manifest_usage", "usage_integrity_unknown")])
def test_historical_and_missing_data_are_conservative(problem, reason):
    task = completed_task()
    if problem == "legacy":
        task = ReviewTask(id="old", pr_url=task.pr_url, status="completed")
    elif problem == "missing":
        task.task_runtime_report = None
    elif problem == "invalid":
        task.task_runtime_report = {"execution_status": "future"}
    elif problem == "snapshot":
        task.run_manifest = None
    elif problem == "identity":
        task.run_manifest.review_id = "other"
    else:
        task.run_manifest.usage_integrity = "unknown"
    outcome = derive_review_outcome(task)
    assert outcome.kind == "inconclusive" and reason in outcome.reason_codes


def test_dismissed_and_duplicate_do_not_block_clean_and_runtime_failure_wins():
    assert derive_review_outcome(completed_task(findings=[{"status": "dismissed"}],
        unscheduled_leads=[{"status": "duplicate"}])).kind == "clean"
    assert derive_review_outcome(completed_task(execution_status="failed")).kind == "failed"
    assert derive_review_outcome(completed_task(execution_status="cancelled")).kind == "cancelled"


def test_dimensions_do_not_conflate_scope_usage_and_dynamic_validation():
    outcome = derive_review_outcome(completed_task(usage_integrity="unknown"))
    assert outcome.finding_conclusion == "no_confirmed_findings"
    assert outcome.review_completeness == "complete" and outcome.runtime_reliability == "unreliable"
    assert outcome.dynamic_validation == "not_executed"
    outcome = derive_review_outcome(completed_task(scope_complete=False))
    assert outcome.review_completeness == "partial" and outcome.runtime_reliability == "reliable"
    assert "budget_exhausted" in derive_review_outcome(completed_task(termination_reason="budget_exhausted")).reason_codes


def test_incomplete_investigation_does_not_invent_abnormal_termination():
    task = completed_task(admitted_investigations=[
        {"status": "insufficient_evidence", "answer": "insufficient_evidence"}])
    task.status = TaskStatus.completed_with_warnings
    outcome = derive_review_outcome(task)
    assert outcome.kind == "inconclusive"
    assert outcome.reason_codes == ["investigation_incomplete"]
    assert outcome.review_completeness == "partial" and outcome.runtime_reliability == "reliable"


@pytest.mark.parametrize("execution,termination", [("running", "running"), ("failed", "technical_failure"),
    ("cancelled", "cancelled"), ("completed", "budget_exhausted"), ("completed", "deadline")])
def test_abnormal_runtime_is_not_hidden_by_completed_task_label(execution, termination):
    outcome = derive_review_outcome(completed_task(execution_status=execution, termination_reason=termination))
    assert "termination_incomplete" in outcome.reason_codes and outcome.runtime_reliability == "unreliable"


def discussion(stats=None, rechecks=None):
    return {"stats": {"status": "complete", "collected": 2, "comments": 3, "referenced": 2,
                      "referenced_comments": 2, "checked": 1, "unchecked": 1, "limitations": [],
                      "termination_reason": "plan_exhausted", **(stats or {})},
            "rechecks": rechecks or []}


def test_historical_discussion_timeout_and_usage_never_degrade_completed_primary_review():
    task = completed_task()
    task.pr_discussion = discussion({"termination_reason": "deadline", "checked": 1, "unchecked": 1,
        "limitations": ["history_recheck:deadline"],
        "usage": {"sent_calls": 1, "settled_tokens": 0, "unknown_attempts": 1,
                  "usage_integrity": "unknown", "total_tokens": None}},
        [{"discussion_id": "inline:1", "status": "not_checked", "reason": "超时"}])
    outcome = derive_review_outcome(task)
    # 主审查结论保持 clean；补充阶段的超时与未知计量只出现在独立字段。
    assert outcome.kind == "clean" and outcome.reason_codes == []
    assert outcome.review_completeness == "complete" and outcome.runtime_reliability == "reliable"
    assert outcome.historical_discussion.status == "timed_out"
    assert outcome.historical_discussion.usage["usage_integrity"] == "unknown"
    assert outcome.historical_discussion.affects_risk is False


@pytest.mark.parametrize("updates,status", [
    ({}, "completed"),
    ({"scope_complete": False}, "completed_with_warnings"),
    ({"usage_integrity": "unknown"}, "completed_with_warnings"),
    ({"findings": [{"status": "candidate"}]}, "completed_with_warnings"),
    ({"execution_status": "failed", "termination_reason": "technical_failure"}, "failed"),
    ({"execution_status": "cancelled", "termination_reason": "cancelled"}, "cancelled"),
])
def test_code_verified_history_preserves_all_primary_review_dimensions(updates, status):
    task = completed_task(**updates)
    task.status = status
    primary = derive_review_outcome(task).model_dump(mode="json")
    task.pr_discussion = discussion(rechecks=[
        {"discussion_id": "inline:1", "path": "src/a.py", "status": "still_present",
         "reason": "当前实现仍返回空值", "evidence_ids": ["e1"], "limitations": []},
        {"discussion_id": "inline:2", "status": "not_checked", "reason": "未核验"}])
    outcome = derive_review_outcome(task)
    combined = outcome.model_dump(mode="json")
    combined.pop("historical_discussion")
    assert combined == primary
    assert "historical_discussion_unresolved" not in outcome.reason_codes
    assert outcome.historical_discussion.affects_risk is True
    assert [item["discussion_id"] for item in outcome.historical_discussion.verified_defects] == ["inline:1"]


def test_unverified_historical_claim_without_evidence_does_not_affect_risk():
    task = completed_task()
    task.pr_discussion = discussion(rechecks=[
        {"discussion_id": "inline:1", "status": "still_present", "reason": "无证据自述",
         "evidence_ids": [], "limitations": []}])
    outcome = derive_review_outcome(task)
    assert outcome.kind == "clean" and outcome.historical_discussion.affects_risk is False
