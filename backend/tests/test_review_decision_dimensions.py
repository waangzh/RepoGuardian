from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.models.publication import PublicationPlan
from app.models.review import TestRunResult as RunResult, ValidationSnapshot
from app.models.review_runtime import DiscussionInvestigationSubmission, DiscussionInvestigationResult
from app.services.publication_planner import PublicationPlanner
from test_review_outcome import completed_task, discussion


@pytest.mark.parametrize("source", ["report", "manifest", "attempts"])
def test_metering_only_never_becomes_a_code_or_merge_gap(source):
    task = completed_task()
    if source == "report":
        task.task_runtime_report["usage_integrity"] = "unknown"
    elif source == "manifest":
        task.run_manifest.usage_integrity = "unknown"
    else:
        task.task_runtime_report["unknown_attempts"] = 1
    plan = PublicationPlanner().build(task)
    assert plan.outcome.kind == "inconclusive"
    assert plan.outcome.coverage == plan.outcome.review_completeness == "complete"
    assert plan.digest.merge_decision.status == "conditional_merge"
    assert plan.digest.merge_decision.unresolved_reasons == []
    for text in (plan.summary_markdown, plan.review_comment_markdown, plan.user_report_markdown):
        assert "已检查范围内未发现已确认问题" in text
        assert "计量" in text


@pytest.mark.parametrize("update", [
    {"admitted_investigations": [{"status": "not_executed"}]},
    {"findings": [{"status": "unresolved"}]}, {"scope_complete": False},
    {"termination_reason": "technical_failure"}, {"execution_status": "failed"},
])
def test_metering_does_not_hide_missing_results_or_incomplete_execution(update):
    plan = PublicationPlanner().build(completed_task(usage_integrity="unknown", **update))
    assert plan.digest.merge_decision.status != "conditional_merge"
    assert plan.digest.merge_decision.label != "已检查范围内未发现已确认问题"


@pytest.mark.parametrize("stage,sha", [("base", "c"), ("head", "d")])
@pytest.mark.parametrize("passed", [True, False])
def test_reference_validations_never_compete_with_current_head(stage, sha, passed):
    task = completed_task()
    original = PublicationPlanner().build(task)
    task.validation_snapshots = [ValidationSnapshot(stage=stage, sha=sha * 40, passed=passed,
        command_results=[RunResult(tool="test_runner", command="pytest", exit_code=0 if passed else 1, passed=passed)])]
    plan = PublicationPlanner().build(task)
    assert plan.digest.validation_state == plan.outcome.dynamic_validation == "not_executed"
    assert plan.digest.merge_decision == original.digest.merge_decision
    assert len(plan.digest.validations) == 1


@pytest.mark.parametrize("severity,status,priority", [
    ("critical", "fix_blockers", "fix_before_merge"), ("high", "fix_blockers", "fix_before_merge"),
    ("medium", "conditional_merge", "assess_improvement"), ("low", "conditional_merge", "assess_improvement"),
    (None, "resolve_unknowns", "assess_impact"), ("invalid", "resolve_unknowns", "assess_impact"),
])
def test_history_is_ranked_by_current_impact_without_rewriting_primary_review(severity, status, priority):
    task = completed_task()
    task.pr_discussion = discussion(rechecks=[{
        "discussion_id": "inline:1", "path": "src/a.py", "status": "still_present",
        "reason": "Current source still allows the original trigger", "evidence_ids": ["e1"],
        "severity": severity, "affected_behavior": "The original caller can reach the affected branch",
        "recommendation": "Guard the affected branch", "limitations": [],
    }])
    task.pr_discussion["links"] = {"inline:1": task.pr_url + "#discussion_r1"}
    original = deepcopy(task.task_runtime_report)
    plan = PublicationPlanner().build(task)
    assert plan.outcome.kind == "clean" and plan.outcome.coverage == "complete"
    assert plan.outcome.reason_codes == [] and plan.outcome.confirmed_count == 0
    assert plan.digest.merge_decision.status == status
    finding = plan.outcome.historical_discussion.findings[0]
    assert finding.priority == priority
    assert finding.discussion_url == task.pr_url + "#discussion_r1"
    assert finding.evidence_ids == ["e1"]
    assert plan.digest.merge_decision.blocking_finding_keys == []
    assert plan.digest.merge_decision.blocking_historical_keys == (["inline:1"] if status == "fix_blockers" else [])
    for text in (plan.summary_markdown, plan.review_comment_markdown, plan.user_report_markdown):
        assert "Guard the affected branch" in text
        # 历史风险章节与当前问题一致使用首字母大写等级；严重度本身必须可见。
        assert finding.severity in text.lower()
    assert PublicationPlan.model_validate_json(plan.model_dump_json()).model_dump(mode="json") == plan.model_dump(mode="json")
    assert task.task_runtime_report == original


def test_legacy_history_unknown_impact_and_unsafe_links_stay_unknown():
    task = completed_task()
    task.pr_discussion = discussion(rechecks=[{
        "discussion_id": "inline:1", "status": "still_present", "reason": "Accepted by the team",
        "evidence_ids": ["e1"], "severity": "high",
    }])
    task.pr_discussion["links"] = {"inline:1": "https://example.test/unsafe"}
    plan = PublicationPlanner().build(task)
    assert plan.digest.merge_decision.status == "resolve_unknowns"
    finding = plan.outcome.historical_discussion.findings[0]
    assert finding.severity == "unknown" and finding.discussion_url is None


def test_history_metadata_requires_valid_schema_and_survives_checkpoint_shape():
    submission = DiscussionInvestigationSubmission(answer="supported", resolution="still_present", reason="Source evidence",
        evidence_ids=["e1"], severity="low", affected_behavior="Hint text differs", recommendation="Adjust hint")
    result = DiscussionInvestigationResult(**submission.model_dump(), work_item_id="w1", proposition="Original hint",
        request_id="r1", attempt_id="a1", snapshot_head_sha="a" * 40)
    assert DiscussionInvestigationResult.model_validate_json(result.model_dump_json()).severity == "low"
    with pytest.raises(ValidationError):
        DiscussionInvestigationSubmission.model_validate({**submission.model_dump(), "severity": "arbitrary"})


@pytest.mark.parametrize("passed", [True, False])
def test_base_results_do_not_override_current_head_results(passed):
    task = completed_task()
    task.validation_snapshots = [ValidationSnapshot(stage=stage, sha=sha * 40, passed=ok,
        command_results=[RunResult(tool="test_runner", command="pytest", exit_code=0 if ok else 1, passed=ok)])
        for stage, sha, ok in [("base", "c", not passed), ("head", "a", passed)]]
    plan = PublicationPlanner().build(task)
    assert plan.outcome.dynamic_validation == ("passed" if passed else "failed")


def test_patch_snapshot_never_claims_current_head_was_checked():
    task = completed_task()
    task.validation_snapshots = [ValidationSnapshot(stage="head", sha="a" * 40, patch_id="patch-1", passed=True,
        command_results=[RunResult(tool="test_runner", command="pytest", exit_code=0, passed=True)])]
    plan = PublicationPlanner().build(task)
    assert plan.outcome.dynamic_validation == "not_executed"
    assert plan.digest.validations[0].stage == "patched"
    assert plan.digest.merge_decision.status == "conditional_merge"


def test_missing_runtime_report_is_not_reclassified_as_metering_only():
    task = completed_task(usage_integrity="unknown")
    task.task_runtime_report = None
    plan = PublicationPlanner().build(task)
    assert plan.outcome.coverage == "unknown"
    assert plan.digest.merge_decision.status != "conditional_merge"


def test_old_frozen_plan_does_not_gain_new_history_fields():
    task = completed_task()
    task.pr_discussion = discussion(rechecks=[{"discussion_id": "inline:1", "status": "still_present",
        "reason": "Original issue", "evidence_ids": ["e1"]}])
    payload = PublicationPlanner().build(task).model_dump(mode="json")
    payload["outcome"]["historical_discussion"].pop("findings")
    for key in ("blocking_historical_keys", "nonblocking_historical_keys"):
        payload["digest"]["merge_decision"].pop(key, None)
    assert PublicationPlan.model_validate(payload).model_dump(mode="json") == payload
