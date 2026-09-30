from copy import deepcopy

import pytest

from app.models.review import ChangedFile, ReviewUnit, ReviewUnitResult, UnitReviewSummary, UnitReviewRecord
from app.services.cross_unit_risk import CrossUnitRiskService
from app.services.review_rebuild import rebuild_task_from_state
from app.graph.nodes.cross_unit_risk import cross_unit_risk_node
from app.services.review_planner import DeterministicReviewPlanner


def _state(paths=("caller.py", "callee.py"), *, edge=True, changed=True):
    units = [ReviewUnit(id=f"u{index}", primary_files=[path], changed_symbols=["load"],
                        estimated_tokens=10, complexity="small", fingerprint=path,
                        grouping_reason="single") for index, path in enumerate(paths)]
    results = [ReviewUnitResult(review_unit_id=unit.id, status="completed", terminal_reason="no_issue",
                               review_summary=UnitReviewSummary(status="reported",
                                   record=UnitReviewRecord(change_summary="修改实现"))) for unit in units]
    edges = [{"source_kind": "symbol", "source": paths[0] + "::load",
              "target_kind": "symbol", "target": paths[1] + "::load", "type": "calls",
              "confidence": 0.92, "parser_id": "tree-sitter.python.v1", "why": "load calls load"}] if edge else []
    return {
        "task_id": "test", "review_units": [item.model_dump(mode="json") for item in units],
        "review_unit_results": [item.model_dump(mode="json") for item in results],
        "changed_files": [{"file_path": path, "change_type": "modified", "additions": 1, "deletions": 1,
                           "hunks": [{"old_start": 1, "old_length": 1, "new_start": 1, "new_length": 1,
                                      "added_lines": [{"line_no": 1, "content": "return None" if changed else "logger.info('x')"}]}]}
                          for path in paths],
        "file_index": [{"path": path, "analysis_level": 2} for path in paths],
        "repository_graph": {"files": [{"path": path} for path in paths], "edges": edges,
                             "metadata": {"file_count": len(paths), "edge_count": len(edges)}},
    }


def test_zero_issues_does_not_skip_changed_contract() -> None:
    state = _state()
    result = CrossUnitRiskService().assess(state)
    assert result.decision == "required"
    assert result.execution_status == "not_implemented"
    assert result.relationships[0].parser_id == "tree-sitter.python.v1"
    assert any(item.code == "changed_contract" for item in result.reasons)
    assert result == CrossUnitRiskService().assess(deepcopy(state))


def test_unchanged_static_relationship_is_not_changed_contract() -> None:
    result = CrossUnitRiskService().assess(_state(changed=False))
    assert result.decision == "skip"
    assert all(item.code != "changed_contract" for item in result.reasons)


@pytest.mark.parametrize("confidence,parser", [(0.3, "tree-sitter.python.v1"), (0.92, "regex.python.v1")])
def test_heuristic_or_weak_edges_only_request_relationship_clarification(confidence, parser) -> None:
    state = _state()
    state["repository_graph"]["edges"][0].update(confidence=confidence, parser_id=parser)
    assert CrossUnitRiskService().assess(state).decision == "uncertain"


def test_independent_changes_and_local_failures_are_not_required() -> None:
    state = _state(edge=False, changed=False)
    assert CrossUnitRiskService().assess(state).decision == "skip"
    state["review_unit_results"][0]["status"] = "failed"
    assert CrossUnitRiskService().assess(state).decision != "required"


def test_related_failure_is_a_dependency_coverage_gap() -> None:
    state = _state(changed=False)
    state["review_unit_results"][0]["status"] = "timed_out"
    result = CrossUnitRiskService().assess(state)
    assert result.decision == "required"
    assert any(item.code == "dependency_coverage_gap" for item in result.reasons)


def test_missing_index_and_legacy_summary_are_unknown_not_independent() -> None:
    state = _state(edge=False)
    state["repository_graph"] = {}
    state["file_index"] = []
    state["review_unit_results"][0].pop("review_summary")
    result = CrossUnitRiskService().assess(state)
    assert result.decision == "uncertain" and result.index_status == "unknown"
    assert any(item.code == "summary_unknown" for item in result.reasons)


def test_unlinked_api_change_requests_relationship_clarification() -> None:
    state = _state(paths=("api/response.py", "frontend/types.ts"), edge=False)
    assert CrossUnitRiskService().assess(state).decision == "uncertain"


def test_unchecked_target_is_not_reported_as_independent_complete_review() -> None:
    state = _state(edge=False)
    state["review_unit_results"][0]["review_summary"]["record"]["target_checks"] = [
        {"target": "检查返回值", "status": "not_checked", "reason": "预算不足"},
    ]
    assert CrossUnitRiskService().assess(state).decision == "uncertain"


def test_unresolved_question_without_scope_does_not_silently_skip() -> None:
    state = _state(edge=False)
    state["review_unit_results"][0]["review_summary"]["record"]["unresolved_questions"] = [
        {"question": "契约责任未知"},
    ]
    assert CrossUnitRiskService().assess(state).decision == "uncertain"


def test_declared_dependency_and_cross_unit_question_are_required() -> None:
    state = _state(edge=False, changed=False)
    state["review_unit_results"][0]["review_summary"]["record"].update(
        contract_dependencies=[{"file_path": "callee.py", "assumption": "调用方保证非空", "status": "unresolved"}],
        unresolved_questions=[{"question": "空值责任在哪一层？", "affected_files": ["callee.py"]}],
    )
    result = CrossUnitRiskService().assess(state)
    assert result.decision == "required"
    assert {item.code for item in result.reasons} >= {"unresolved_dependency", "unresolved_cross_unit_question"}


def test_split_target_symbol_does_not_attach_to_unrelated_unit() -> None:
    state = _state()
    extra = deepcopy(state["review_units"][1])
    extra.update(id="u2", changed_symbols=["delete"])
    state["review_units"].append(extra)
    result = CrossUnitRiskService().assess(state)
    assert all(item.target_unit_id != "u2" for item in result.relationships)


def test_contract_change_in_another_hunk_does_not_trigger_current_unit() -> None:
    state = _state(changed=False)
    state["changed_files"][0]["hunks"].append({
        "old_start": 10, "old_length": 1, "new_start": 10, "new_length": 1,
        "added_lines": [{"line_no": 10, "content": "def unrelated():"}],
    })
    file = ChangedFile.model_validate(state["changed_files"][0])
    state["review_units"][0]["diff_hunk_ids"] = [DeterministicReviewPlanner.hunk_id(
        file.file_path, 0, file.hunks[0].model_dump(mode="json"),
    )]
    result = CrossUnitRiskService().assess(state)
    assert result.decision == "skip"
    assert all(item.code != "changed_contract" for item in result.reasons)


@pytest.mark.asyncio
async def test_node_preserves_issues_and_original_coverage_and_roundtrips_task() -> None:
    state = _state()
    state.update(review_issues=[], review_coverage={"eligible_files": 2}, step_progress=[])
    before = deepcopy(state)
    update = await cross_unit_risk_node(state)
    assert state == before
    assert "review_issues" not in update and "review_coverage" not in update
    task = rebuild_task_from_state({**state, **update})
    assert task.cross_unit_risk.decision == "required"
    assert task.coverage.eligible_files == 2
    assert task.followup_results == []
