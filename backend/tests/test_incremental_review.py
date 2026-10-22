"""增量审查：复用判定、计划合并、前序问题重定位与确认绑定。"""

from datetime import datetime, timezone
import pytest

from app.models.incremental import IncrementalReviewBinding
from app.models.review import (
    ChangedFile,
    PullRequestInfo,
    DiffHunk,
    DiffLine,
    IssueStatus,
    ReviewPlan,
    ReviewRunManifest,
    ReviewTask,
    ReviewUnit,
    ReviewUnitComplexity,
    ReviewUnitResult,
    ReviewUnitStatus,
    UnitReviewRecord,
    UnitReviewSummary,
)
from app.services import incremental_review as incremental
from app.services.review_outcome import derive_review_outcome


def _unit(identity: str, files: list[str]) -> ReviewUnit:
    return ReviewUnit(id=identity, primary_files=files, fingerprint="f" * 64,
                      estimated_tokens=10, complexity=ReviewUnitComplexity.small,
                      grouping_reason="single_file")


def _result(identity: str) -> ReviewUnitResult:
    return ReviewUnitResult(
        review_unit_id=identity, status=ReviewUnitStatus.completed, terminal_reason="completed",
        review_summary=UnitReviewSummary(status="reported", record=UnitReviewRecord(
            change_summary="检查完成"), input_protocol="canonical-evidence-v3"))


def _changed(path: str, added: str = "x = 2") -> ChangedFile:
    line = DiffLine(kind="added", content=added, new_line_no=2)
    hunk = DiffHunk(old_start=1, old_length=1, new_start=1, new_length=2,
                    hunk_id="hunk-" + path.replace("/", "-"), lines=[line],
                    added_lines=[], removed_lines=[])
    return ChangedFile(file_path=path, change_type="modified", additions=1, deletions=0,
                       hunks=[hunk])


def _previous(units: list[ReviewUnit], results: list[ReviewUnitResult], *,
              findings=None) -> ReviewTask:
    task = ReviewTask(id="a" * 32, pr_url="https://github.com/o/r/pull/1", engine="task_review_v1",
                      status="completed", model="fixture", pr=PullRequestInfo.model_validate(PR_INFO),
                      task_runtime_configuration={"model": "fixture", "provider": "openai-compatible",
                                                  "endpoint": "https://example.test/v1",
                                                  "prompt_version": "v", "policy_version": "p",
                                                  "effective_plan": {"depth": "standard",
                                                                     "focus": ["correctness"],
                                                                     "include_pr_discussions": False}},
                      review_units=units, review_unit_results=results,
                      task_runtime_report={"findings": findings or []})
    task.run_manifest = ReviewRunManifest(
        review_id=task.id, engine="task_review_v1", coverage_version="task-goals-v1",
        head_sha="b" * 40, base_sha="c" * 40, effective_base_sha="c" * 40,
        provider="openai-compatible", model="fixture",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        completed_at=datetime(2026, 1, 1, tzinfo=timezone.utc), duration_ms=0,
        coverage=task.coverage)
    return task


CONFIGURATION = {
    "model": "fixture", "provider": "openai-compatible", "endpoint": "https://example.test/v1",
    "prompt_version": "v", "policy_version": "p",
    "effective_plan": {"depth": "standard", "focus": ["correctness"],
                       "include_pr_discussions": False},
}
PR_INFO = {"owner": "o", "repo": "r", "number": 1, "title": "t", "body": "b",
           "html_url": "https://github.com/o/r/pull/1", "clone_url": "https://github.com/o/r.git",
           "base": {"ref": "main", "sha": "c" * 40, "repo_clone_url": "https://github.com/o/r.git"},
           "head": {"ref": "f", "sha": "d" * 40, "repo_clone_url": "https://github.com/o/r.git"}}


def test_reuses_only_unchanged_files_and_keeps_full_diff_for_changed_files():
    previous = _previous([_unit("ru-a", ["a.py"]), _unit("ru-b", ["b.py"])],
                         [_result("ru-a"), _result("ru-b")])
    full = [_changed("a.py"), _changed("b.py", "y = 2")]
    decision = incremental.plan_incremental(
        previous=previous, pr_url=previous.pr_url, pr_info=PR_INFO,
        effective_base_sha="c" * 40, new_head_sha="d" * 40, full_changed=full,
        full_plan_units=[_unit("ru-a", ["a.py"]), _unit("ru-b", ["b.py"])],
        changed_since_previous=["b.py"], configuration=CONFIGURATION, model="fixture",
        ancestor=True, same_merge_base=True)
    assert decision.applicable and decision.binding is not None
    assert [unit.id for unit in decision.reused_units] == ["ru-a"]
    assert decision.binding.delta_paths == ["b.py"]
    assert decision.preview.reused_unit_count == 1 and decision.preview.delta_file_count == 1


@pytest.mark.parametrize("change,reason", [
    ({"ancestor": False}, "history_rewritten"),
    ({"same_merge_base": False}, "content_base_changed"),
    ({"changed_since_previous": None}, "delta_diff_unavailable"),
    ({"changed_since_previous": []}, "no_incremental_changes"),
    ({"model": "other"}, "model_changed"),
    ({"pr_info": {**PR_INFO, "title": "changed"}}, "pr_description_changed"),
    ({"configuration": {**CONFIGURATION, "prompt_version": "v2"}}, "review_configuration_changed"),
])
def test_any_identity_drift_falls_back_to_full_review(change, reason):
    previous = _previous([_unit("ru-a", ["a.py"])], [_result("ru-a")])
    values = {"previous": previous, "pr_url": previous.pr_url, "pr_info": PR_INFO,
              "effective_base_sha": "c" * 40, "new_head_sha": "d" * 40,
              "full_changed": [_changed("a.py")],
              "full_plan_units": [_unit("ru-a", ["a.py"])],
              "changed_since_previous": ["b.py"], "configuration": CONFIGURATION,
              "model": "fixture", "ancestor": True, "same_merge_base": True}
    values.update(change)
    decision = incremental.plan_incremental(**values)
    assert not decision.applicable and decision.reason == reason
    assert decision.preview.status == "not_applicable"


def test_changed_file_unit_is_not_reused_even_if_other_unit_is():
    previous = _previous([_unit("ru-a", ["a.py"]), _unit("ru-b", ["b.py"])],
                         [_result("ru-a"), _result("ru-b")])
    decision = incremental.plan_incremental(
        previous=previous, pr_url=previous.pr_url, pr_info=PR_INFO,
        effective_base_sha="c" * 40, new_head_sha="d" * 40,
        full_changed=[_changed("a.py"), _changed("b.py")],
        full_plan_units=[_unit("ru-a", ["a.py"]), _unit("ru-b", ["b.py"])],
        changed_since_previous=["a.py"], configuration=CONFIGURATION, model="fixture",
        ancestor=True, same_merge_base=True)
    assert [unit.id for unit in decision.reused_units] == ["ru-b"]


def test_merge_plan_removes_duplicate_ownership_and_rejects_conflict():
    plan = ReviewPlan(planner_version="v", review_units=[_unit("ru-a", ["a.py"]),
                                                         _unit("ru-b", ["b.py"])])
    merged = incremental.merge_incremental_plan(plan, [_unit("prev-a", ["a.py"])], ["w"])
    assert {unit.id for unit in merged.review_units} == {"prev-a", "ru-b"}
    assert "w" in merged.warnings
    # 部分重叠的 Unit 会被保留，从而与复用 Unit 争用同一主审文件；必须拒绝。
    with pytest.raises(ValueError, match="primary_conflict"):
        incremental.merge_incremental_plan(
            ReviewPlan(planner_version="v", review_units=[_unit("ru-a", ["a.py", "b.py"])]),
            [_unit("prev-a", ["a.py"])], [])


def test_reused_unit_result_must_belong_to_bound_previous_task():
    previous = _previous([_unit("ru-a", ["a.py"])], [_result("ru-a")])
    binding = IncrementalReviewBinding(
        previous_task_id=previous.id, previous_head_sha="b" * 40, review_base_sha="c" * 40,
        effective_base_sha="c" * 40, new_head_sha="d" * 40, diff_hash="e" * 64,
        delta_paths=["b.py"], delta_file_count=1, pr_file_count=2,
        reused_units=[{"unit_id": "ru-a", "source_task_id": previous.id}])
    results = incremental.reuse_results(binding, previous)
    assert results[0].reused_from_task_id == previous.id
    assert results[0].reused_from_head_sha == "b" * 40
    with pytest.raises(ValueError, match="previous_task_missing"):
        incremental.reuse_results(binding, None)


def test_carry_forward_relocates_evidence_and_keeps_publication_withheld():
    from app.models.review import (CommentPlacement, EvidenceAnchor, EvidenceResolutionMethod,
                                   EvidenceResolutionStatus, IssueCategory, ReviewIssue, Severity)

    anchor = EvidenceAnchor(file_path="a.py", existing_code="    return 1",
                            resolved_start_line=2, resolved_end_line=2,
                            resolution_method=EvidenceResolutionMethod.diff_exact,
                            resolution_status=EvidenceResolutionStatus.exact,
                            resolved_side="head", match_count=1, anchor_hash="h" * 64)
    issue = ReviewIssue(id="issue-1", review_unit_id="ru-a", title="示例问题", category=IssueCategory.correctness,
                        severity=Severity.medium, confidence=0.9, affected_behavior="受影响的行为",
                        failure_scenario="触发场景", recommendation="修复建议", primary_evidence=anchor,
                        status=IssueStatus.confirmed, placement=CommentPlacement.summary)
    record = {"status": "confirmed", "candidate": {"candidate_id": "issue-1", "claim": "示例问题",
              "primary_anchor": "g", "supporting_evidence": ["e"], "source_result_ids": ["r"]},
              "issue": issue.model_dump(mode="json"),
              "publication": {"version": "claim-publication-v1", "head_sha": "b" * 40}}
    previous = _previous([_unit("ru-a", ["a.py"])], [_result("ru-a")], findings=[record])
    binding = IncrementalReviewBinding(
        previous_task_id=previous.id, previous_head_sha="b" * 40, review_base_sha="c" * 40,
        effective_base_sha="c" * 40, new_head_sha="d" * 40, diff_hash="e" * 64,
        delta_paths=["b.py"], delta_file_count=1, pr_file_count=2,
        reused_units=[{"unit_id": "ru-a", "source_task_id": previous.id}])
    carried = incremental.carry_forward_findings(
        previous=previous, binding=binding, plan_units=[_unit("ru-a", ["a.py"])],
        file_loader=lambda path, side: "def f():\n    return 1\n")
    assert len(carried) == 1
    assert carried[0]["carried_forward"]["requires_current_reverification"] is True
    # 原 publication 绑定旧 Head，保留后由发布准入扣留，不能直接发布。
    assert carried[0]["publication"]["head_sha"] == "b" * 40
    # 当前快照中找不到原文时不沿用。
    missing = incremental.carry_forward_findings(
        previous=previous, binding=binding, plan_units=[_unit("ru-a", ["a.py"])],
        file_loader=lambda path, side: "def f():\n    return 2\n")
    assert missing == []


def test_incremental_binding_is_recorded_in_manifest_and_outcome():
    from datetime import datetime, timezone

    from app.services.review_manifest import build_review_manifest

    task = _previous([_unit("ru-a", ["a.py"])], [_result("ru-a")])
    task.incremental = {"version": "incremental-review-v1"}
    task.task_runtime_report = {
        "execution_status": "completed", "termination_reason": "plan_exhausted",
        "scope_complete": True, "initial_scope_coverage": [{"status": "examined"}],
        "usage_integrity": "complete", "findings": []}
    outcome = derive_review_outcome(task)
    # 增量来源是执行方式，不是证据缺口；结论完整性仍由目标覆盖决定。
    assert outcome.review_completeness == "complete"
    manifest = build_review_manifest({
        "task_id": task.id, "engine": "task_review_v1", "pr_url": task.pr_url,
        "status": "completed", "phase": "completed",
        "pr_info": PR_INFO, "changed_files": [], "review_units": [], "review_unit_results": [],
        "review_issues": [], "review_coverage": {"coverage_version": "task-goals-v1",
            "changed_files": 0, "eligible_files": 1, "reviewed_files": 1, "coverage_rate": 1,
            "completed_units": 1, "total_units": 1, "unit_coverage_rate": 1},
        "task_runtime_report": task.task_runtime_report,
        "task_runtime_configuration": {"incremental": task.incremental},
        "model_usages": [], "excluded_files": [], "warnings": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }, datetime.now(timezone.utc))
    assert manifest.incremental == task.incremental
