"""增量审查：复用判定、计划合并、前序问题重定位与确认绑定。"""

from datetime import datetime, timezone
import pytest

from app.models.incremental import IncrementalReviewBinding
from app.models.review import (
    ChangedFile,
    PullRequestInfo,
    DiffHunk,
    DiffLine,
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
from app.services.review import incremental_review as incremental
from app.services.review.review_outcome import derive_review_outcome
from app.review.runtime_goals import plan_runtime_goals
from tests.support.runtime_harness import production_input as production_input
from tests.support.persistence import persistence as persistence
from tests.support.review_plans import plan_service as plan_service


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
    task.changed_files = [_changed(path) for unit in units for path in unit.primary_files]
    goals = plan_runtime_goals(task.changed_files, ReviewPlan(planner_version="v", review_units=units))
    task.task_runtime_report = {
        "execution_status": "completed", "termination_reason": "plan_exhausted",
        "scope_complete": True, "usage_integrity": "complete", "findings": findings or [],
        "initial_scope_coverage": [
            {**goal.model_dump(mode="json"), "status": "examined", "result_id": "result-" + goal.goal_id}
            for goal in goals],
        "reuse_dependencies": {unit.id: {"paths": unit.primary_files, "repository_discovery": False}
                               for unit in units},
    }
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


def same_snapshot_input():
    from copy import deepcopy
    previous = _previous([_unit("ru-a", ["a.py"])], [])
    configuration = deepcopy(CONFIGURATION)
    configuration["stability_contract"] = "review-stability-v1"
    previous.task_runtime_configuration = deepcopy(configuration)
    previous.task_runtime_report["admitted_investigations"] = [{
        "work_item_id": "i", "unit_id": "ru-a", "status": "answered", "answer": "supported"}]
    previous.task_runtime_report["reuse_dependencies"]["ru-a"]["repository_discovery"] = True
    return {"previous": previous, "pr_url": previous.pr_url, "pr_info": PR_INFO,
            "effective_base_sha": "c" * 40, "new_head_sha": "b" * 40,
            "full_changed": previous.changed_files, "full_plan_units": previous.review_units,
            "changed_since_previous": [], "configuration": configuration,
            "model": "fixture", "ancestor": True, "same_merge_base": True}


def test_same_snapshot_reuse_is_explicit_and_revalidates_bound_completed_work():
    values = same_snapshot_input()
    independent = incremental.plan_incremental(**values)
    assert not independent.applicable and independent.reason == "same_head"
    decision = incremental.plan_incremental(**values, allow_same_snapshot=True)
    assert decision.applicable and decision.preview.delta_file_count == 0
    assert "未重新执行" in decision.preview.message
    coverage = incremental.reuse_coverage(decision.binding, values["previous"],
        values["full_changed"], values["full_plan_units"], delta_paths=[], repository_graph={})
    assert coverage and all(item["reused_from_task_id"] == values["previous"].id
                            for item in coverage.values())


@pytest.mark.parametrize("change", [
    "legacy", "unresolved", "unknown_usage", "description", "base", "strategy", "diff", "findings",
    "planner_input", "request_timeout",
])
def test_same_snapshot_reuse_rejects_uncertain_or_changed_inputs(change):
    values = same_snapshot_input()
    previous = values["previous"]
    if change == "legacy":
        previous.task_runtime_configuration.pop("stability_contract")
    elif change == "unresolved":
        previous.task_runtime_report["admitted_investigations"][0]["answer"] = "insufficient_evidence"
    elif change == "unknown_usage":
        previous.task_runtime_report["usage_integrity"] = "unknown"
    elif change == "description":
        values["pr_info"] = {**PR_INFO, "body": "different"}
    elif change == "base":
        values["effective_base_sha"] = "e" * 40
    elif change == "strategy":
        values["configuration"]["prompt_version"] = "different"
    elif change == "planner_input":
        values["configuration"]["initial_plan_hash"] = "different"
    elif change == "request_timeout":
        values["configuration"]["request_timeout_seconds"] = 120
    elif change == "diff":
        values["full_changed"] = [_changed("a.py", "different")]
    elif change == "findings":
        previous.task_runtime_report["findings"] = [{"status": "confirmed"}]
    assert not incremental.plan_incremental(**values, allow_same_snapshot=True).applicable


@pytest.mark.parametrize("with_finding", [False, "initial", "derived"])
async def test_same_snapshot_runtime_reuses_coverage_without_new_model_calls(production_input, persistence, with_finding):
    from copy import deepcopy
    from app.graph.nodes.task_review_runtime import task_review_runtime_node
    from app.models.review import ReviewUnit as Unit
    from app.services.review.review_manifest import build_review_manifest

    state, provider, calls = production_input
    repository = persistence[0]
    state["task_id"] = "a" * 32
    state["effective_base_sha"] = state["base_sha"]
    state["pr_info"] = deepcopy(PR_INFO)
    state["pr_info"]["base"]["sha"] = state["base_sha"]
    state["pr_info"]["head"]["sha"] = state["head_sha"]
    state["task_runtime_configuration"]["stability_contract"] = (
        "review-stability-v2" if with_finding else "review-stability-v1")
    state.update(_provider=provider, _coordination_repository=repository)
    repository.create_task(ReviewTask(id=state["task_id"], pr_url=state["pr_url"],
        engine="task_review_v1", task_runtime_configuration=state["task_runtime_configuration"]))
    first = await task_review_runtime_node(state)
    previous = ReviewTask(id=state["task_id"], pr_url=state["pr_url"], engine="task_review_v1",
        status="completed", model="fixture", pr=PullRequestInfo.model_validate(state["pr_info"]),
        changed_files=[ChangedFile.model_validate(value) for value in state["changed_files"]],
        review_units=[Unit.model_validate(value) for value in state["review_plan"]["review_units"]],
        task_runtime_configuration=state["task_runtime_configuration"],
        task_runtime_report=first["task_runtime_report"],
        run_manifest=build_review_manifest({**state, **first}, datetime.now(timezone.utc)))
    if with_finding:
        from types import SimpleNamespace
        from app.models.review_runtime import Candidate, VerificationResult
        from app.services.publication.review_publication import publication_view, verification_subject_hash, publication_issue
        from tests.support.review_tasks import publishable_task as publishable_task
        record = deepcopy(publishable_task().task_runtime_report["findings"][0])
        record["candidate"]["primary_anchor"] = ("derived-source" if with_finding == "derived" else
            previous.task_runtime_report["initial_scope_coverage"][0]["goal_id"])
        candidate = Candidate.model_validate(record["candidate"])
        spec = SimpleNamespace(snapshot_head_sha=state["head_sha"], snapshot_base_sha=state["base_sha"])
        record["verification"].update(snapshot_head_sha=state["head_sha"],
            subject_hash=verification_subject_hash(candidate, spec))
        record["publication"] = publication_view(candidate, VerificationResult.model_validate(record["verification"]),
            spec, usage_known=True)
        issue = record["issue"]
        issue["review_unit_id"] = previous.review_units[0].id
        issue["primary_evidence"].update(file_path="a.py", existing_code="return 2", resolved_start_line=2, resolved_end_line=2)
        issue["resolved_location"].update(file_path="a.py", start_line=2, end_line=2)
        previous.task_runtime_report["findings"] = [record]
        assert publication_issue(previous, record)[0] is not None
    repository.save_task(previous)
    configuration = deepcopy(state["task_runtime_configuration"])
    decision = incremental.plan_incremental(
        previous=previous, pr_url=state["pr_url"], pr_info=state["pr_info"],
        effective_base_sha=state["base_sha"], new_head_sha=state["head_sha"],
        full_changed=previous.changed_files, full_plan_units=previous.review_units,
        changed_since_previous=[], configuration=configuration, model="fixture",
        ancestor=True, same_merge_base=True, allow_same_snapshot=True)
    assert decision.applicable, decision.reason
    configuration.update(incremental=decision.binding.model_dump(mode="json"),
        incremental_units=[unit.model_dump(mode="json") for unit in decision.reused_units])
    current = {**state, "task_id": "b" * 32, "task_runtime_configuration": configuration}
    repository.create_task(ReviewTask(id=current["task_id"], pr_url=current["pr_url"],
        engine="task_review_v1", task_runtime_configuration=configuration))
    output = await task_review_runtime_node(current)
    assert len(calls) == 1 and output["task_runtime_report"]["sent_calls"] == 0
    assert output["task_runtime_report"]["scope_complete"]
    assert all(goal["reused_from_task_id"] == previous.id
               for goal in output["task_runtime_report"]["initial_scope_coverage"])
    if with_finding:
        findings = output["task_runtime_report"]["findings"]
        assert len(findings) == 1 and findings[0]["status"] == "confirmed"
        assert findings[0]["origin_review_id"] == previous.id
        assert findings[0]["publication"] == previous.task_runtime_report["findings"][0]["publication"]
        current_task = previous.model_copy(update={"id": current["task_id"], "task_runtime_report": output["task_runtime_report"]})
        assert publication_issue(current_task, findings[0])[0] is not None
    invalid = deepcopy(configuration)
    invalid["deadline_seconds"] += 1
    rejected = {**state, "task_id": "c" * 32, "task_runtime_configuration": invalid}
    repository.create_task(ReviewTask(id=rejected["task_id"], pr_url=rejected["pr_url"],
        engine="task_review_v1", task_runtime_configuration=invalid))
    with pytest.raises(ValueError, match="same_snapshot_configuration_changed"):
        await task_review_runtime_node(rejected)
    assert len(calls) == 1


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
        previous_report_hash=incremental.stable_hash(previous.task_runtime_report),
        reused_units_hash=incremental.stable_hash([previous.review_units[0].model_dump(mode="json")]),
        delta_paths=["b.py"], delta_file_count=1, pr_file_count=2,
        reused_units=[{"unit_id": "ru-a", "source_task_id": previous.id}])
    full = [_changed("a.py"), _changed("b.py")]
    binding.diff_hash = incremental.stable_hash({
        "full": [file.model_dump(mode="json") for file in full], "delta": ["b.py"]})
    kwargs = {"full_changed": full, "plan_units": previous.review_units,
              "delta_paths": ["b.py"], "repository_graph": {}}
    coverage = incremental.reuse_coverage(binding, previous, **kwargs)
    assert all(item["reused_from_task_id"] == previous.id for item in coverage.values())
    assert all(item["reused_from_head_sha"] == "b" * 40 for item in coverage.values())
    with pytest.raises(ValueError, match="previous_task_missing"):
        incremental.reuse_coverage(binding, None, **kwargs)


@pytest.mark.parametrize("status", ["confirmed", "published", "candidate", "unresolved", "future"])
def test_previous_findings_require_current_reverification(status):
    previous = _previous([_unit("ru-a", ["a.py"])], [], findings=[{"status": status}])
    decision = incremental.plan_incremental(
        previous=previous, pr_url=previous.pr_url, pr_info=PR_INFO,
        effective_base_sha="c" * 40, new_head_sha="d" * 40,
        full_changed=[_changed("a.py"), _changed("b.py")],
        full_plan_units=[_unit("ru-a", ["a.py"]), _unit("ru-b", ["b.py"])],
        changed_since_previous=["b.py"], configuration=CONFIGURATION, model="fixture",
        ancestor=True, same_merge_base=True)
    assert not decision.applicable
    assert decision.reason == "previous_findings_require_reverification"


def test_incremental_binding_is_recorded_in_manifest_and_outcome():
    from datetime import datetime, timezone

    from app.services.review.review_manifest import build_review_manifest

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


@pytest.mark.parametrize("case", [
    "related", "read", "graph", "discovery", "missing", "partial", "invalid_goal", "policy",
])
def test_dependency_changes_and_unknown_coverage_are_not_reused(case):
    previous = _previous([_unit("ru-a", ["a.py"])], [])
    graph = {}
    delta = {"outside.py"}
    if case == "related":
        previous.review_units[0].related_files = ["outside.py"]
    elif case == "read":
        previous.task_runtime_report["reuse_dependencies"]["ru-a"]["paths"].append("outside.py")
    elif case == "graph":
        graph = {"edges": [{"source_file": "a.py", "target_file": "outside.py", "type": "calls"}]}
    elif case == "discovery":
        previous.task_runtime_report["reuse_dependencies"]["ru-a"]["repository_discovery"] = True
    elif case == "missing":
        previous.task_runtime_report.pop("reuse_dependencies")
    elif case == "partial":
        previous.task_runtime_report["initial_scope_coverage"][0]["status"] = "not_examined"
    elif case == "invalid_goal":
        previous.task_runtime_report["initial_scope_coverage"][0].pop("goal_id")
    else:
        previous.review_units[0].rule_ids = ["changed-rule"]
    assert incremental.reusable_units(
        previous, [_changed("a.py")], delta, [_unit("ru-a", ["a.py"])], graph) == []


async def test_production_runtime_reuses_current_coverage_and_preserves_accounting(
        production_input, persistence):
    import copy
    import subprocess
    from pathlib import Path

    from app.graph.nodes.review_units import review_plan_node
    from app.graph.nodes.task_review_runtime import task_review_runtime_node
    from app.models.review import ReviewUnit as Unit
    from app.services.review.review_manifest import build_review_manifest
    from app.tools.diff_parser import DiffParser

    state, provider, calls = production_input
    state["task_id"] = "a" * 32
    state["effective_base_sha"] = state["base_sha"]
    state["diff_policy"] = "merge_base"
    state["task_runtime_configuration"]["prompt_version"] = "task-review-prompt-v8"
    state["pr_info"] = copy.deepcopy(PR_INFO)
    state["pr_info"]["base"]["sha"] = state["base_sha"]
    state["pr_info"]["head"]["sha"] = state["head_sha"]
    repository = persistence[0]
    state.update(_provider=provider, _coordination_repository=repository)

    def persist(current, output):
        task = ReviewTask(
            id=current["task_id"], pr_url=current["pr_url"], engine="task_review_v1",
            status="completed", model="fixture",
            pr=PullRequestInfo.model_validate(current["pr_info"]),
            changed_files=[ChangedFile.model_validate(item) for item in current["changed_files"]],
            review_units=[Unit.model_validate(item) for item in current["review_plan"]["review_units"]],
            task_runtime_configuration=current["task_runtime_configuration"],
            task_runtime_report=output["task_runtime_report"],
            run_manifest=build_review_manifest({**current, **output}, datetime.now(timezone.utc)))
        repository.save_task(task)
        return repository.get_task(task.id)

    repository.create_task(ReviewTask(id=state["task_id"], pr_url=state["pr_url"],
        engine="task_review_v1", task_runtime_configuration=state["task_runtime_configuration"]))
    first_output = await task_review_runtime_node(state)
    assert first_output["task_runtime_report"]["scope_complete"]
    previous = persist(state, first_output)
    assert previous.review_unit_results == []
    assert previous.task_runtime_report["reuse_dependencies"]
    root = Path(state["repo_path"])

    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              check=True, encoding="utf-8").stdout.strip()

    for index in (1, 2, 3):
        source = "\n".join(f"value{i} = {i}" for i in range(40))
        if index == 3:
            git("rm", "b.py")
        else:
            (root / "b.py").write_text(source + f"\ndef second():\n    return 2\nflag = {index}\n", encoding="utf-8")
            git("add", "b.py")
        git("commit", "-m", f"increment-{index}")
        new_head = git("rev-parse", "HEAD")
        changed = DiffParser().parse(git("diff", "--unified=3", state["base_sha"], new_head))
        configuration = copy.deepcopy(state["task_runtime_configuration"])
        configuration.pop("incremental", None)
        configuration.pop("incremental_units", None)
        current = {**state, "task_id": str(index) * 32, "head_sha": new_head,
                   "changed_files": [file.model_dump(mode="json") for file in changed],
                   "task_runtime_configuration": configuration}
        current["pr_info"] = copy.deepcopy(state["pr_info"])
        current["pr_info"]["head"]["sha"] = new_head
        current.update(await review_plan_node(current))
        full_plan = ReviewPlan.model_validate(current["review_plan"])
        decision = incremental.plan_incremental(
            previous=previous, pr_url=state["pr_url"], pr_info=current["pr_info"],
            effective_base_sha=state["base_sha"], new_head_sha=new_head, full_changed=changed,
            full_plan_units=full_plan.review_units,
            changed_since_previous=["b.py"], configuration=configuration, model="fixture",
            ancestor=True, same_merge_base=True)
        assert decision.applicable, decision.reason
        configuration.update(
            incremental=decision.binding.model_dump(mode="json"),
            incremental_units=[unit.model_dump(mode="json") for unit in decision.reused_units])
        current.update(await review_plan_node(current))
        repository.create_task(ReviewTask(id=current["task_id"], pr_url=current["pr_url"],
            engine="task_review_v1", task_runtime_configuration=configuration))
        output = await task_review_runtime_node(current)
        report = output["task_runtime_report"]
        expected_calls = 0 if index == 3 else 1
        assert report["scope_complete"] and report["sent_calls"] == expected_calls
        assert report["settled_tokens"] == 15 * expected_calls
        assert len(output["model_usages"]) == expected_calls
        assert report["readable_scope_hash"] is not None
        assert len(report["initial_scope_coverage"]) == len(plan_runtime_goals(
            changed, ReviewPlan.model_validate(current["review_plan"])))
        reused = [item for item in report["initial_scope_coverage"] if item.get("reused")]
        assert reused and all(item["target_ref"] == "a.py" and item["result_id"] for item in reused)
        assert all(item["reused_from_task_id"] == previous.id for item in reused)
        assert not any(item.get("reused") for item in report["initial_scope_coverage"]
                       if item["target_ref"] == "b.py")
        assert output["review_unit_results"] == [] and report["findings"] == []
        repeated = await task_review_runtime_node(current)
        assert repeated["task_runtime_report"]["initial_scope_coverage"] == report["initial_scope_coverage"]
        assert len(calls) == min(index + 1, 3)
        previous = persist(current, output)
        state = current

    bound = repository.get_task(state["task_runtime_configuration"]["incremental"]["previous_task_id"])
    bound.task_runtime_report["findings"] = [{"status": "confirmed"}]
    repository.save_task(bound)
    with pytest.raises(ValueError, match="previous_report_changed"):
        await task_review_runtime_node(state)


async def test_preview_confirmation_and_execution_use_the_same_incremental_binding(plan_service):
    import copy
    import subprocess
    from pathlib import Path
    from unittest.mock import AsyncMock

    from app.graph.nodes.review_units import review_plan_node
    from app.graph.nodes.task_review_runtime import task_review_runtime_node
    from app.models.review import ReviewPreviewRequest
    from app.models.review_plan import ReviewPlanOptions
    from app.services.preview.preview_confirmation import validate_confirmation
    from app.services.review.review_manifest import build_review_manifest
    from app.services.review.review_rebuild import rebuild_task_from_state
    from tests.support.review_plans import confirmation

    service, state, provider, calls, pr = plan_service
    request = ReviewPreviewRequest(pr_url=pr.html_url,
                                   plan=ReviewPlanOptions(include_pr_discussions=False))

    async def input_state(task, current_pr):
        prepared = await service._prepare_preview_inputs(current_pr, task.task_runtime_configuration, None, None)
        return {**state, **prepared["index"], **prepared["diff_context"],
                "task_id": task.id, "created_at": task.created_at.isoformat(),
                "model": task.task_runtime_configuration["model"],
                "head_sha": current_pr.head.sha, "base_sha": current_pr.base.sha,
                "effective_base_sha": task.task_runtime_configuration["snapshot"]["effective_base_sha"],
                "pr_info": current_pr.model_dump(mode="json"),
                "changed_files": [file.model_dump(mode="json") for file in
                                  service._diff_parser.parse(prepared["diff"])],
                "task_runtime_configuration": task.task_runtime_configuration,
                "_provider": provider, "_coordination_repository": service._repository}

    first_preview = await service.preview(request)
    first = await service.create_confirmed_task(confirmation(first_preview))
    first_state = await input_state(first, pr)
    first_state.update(await review_plan_node(first_state))
    first_output = await task_review_runtime_node(first_state)
    assert first_output["task_runtime_report"]["scope_complete"]
    saved_state = {**first_state, **first_output, "phase": "completed"}
    saved = rebuild_task_from_state(saved_state)
    saved.run_manifest = build_review_manifest(saved_state, datetime.now(timezone.utc))
    service._repository.save_task(saved)

    root = Path(state["repo_path"])
    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              check=True, encoding="utf-8").stdout.strip()
    (root / "b.py").write_text(
        "\n".join(f"value{i} = {i}" for i in range(40)) + "\ndef other():\n    return 2\n", encoding="utf-8")
    git("add", "b.py")
    git("commit", "-m", "next")
    next_pr = pr.model_copy(deep=True)
    next_pr.head.sha = git("rev-parse", "HEAD")
    service._github_tool.fetch_pr = AsyncMock(return_value=next_pr)
    service._github_tool.compare_delta = lambda owner, repo, base, head: {
        "status": "ahead", "files": service._git_tool.changed_paths(root, base, head)}
    service._git_tool.clone_and_diff = lambda *args, **kwargs: (
        str(root), git("diff", "--unified=3", pr.base.sha, next_pr.head.sha))
    second_preview = await service.preview(request)
    assert second_preview.incremental.status == "applied", second_preview.incremental.reason
    record = service._repository.get_preview(second_preview.preview_id)
    validate_confirmation(record, confirmation(second_preview))
    for kind in ("binding", "units", "removed"):
        broken = copy.deepcopy(record)
        if kind == "binding":
            broken["configuration"]["incremental"]["previous_head_sha"] = "f" * 40
        elif kind == "units":
            broken["configuration"]["incremental_units"][0]["estimated_tokens"] += 1
        else:
            broken["configuration"].pop("incremental")
            broken["configuration"].pop("incremental_units")
        with pytest.raises(ValueError, match="review_plan_stale"):
            validate_confirmation(broken, confirmation(second_preview))
    second = await service.create_confirmed_task(confirmation(second_preview))
    current = await input_state(second, next_pr)
    current.update(await review_plan_node(current))
    output = await task_review_runtime_node(current)
    assert output["task_runtime_report"]["scope_complete"]
    assert any(goal.get("reused") for goal in output["task_runtime_report"]["initial_scope_coverage"])
    assert output["task_runtime_report"]["sent_calls"] == 1 and len(calls) == 2
