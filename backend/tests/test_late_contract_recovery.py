import pytest

from app.models.review import AgentAction, ExecutionBudget, UnitReviewResponse
from app.services.review_unit_executor import ReviewUnitExecutor
from app.services.unit_review_summary import build_record_input, validate_record
from app.services.unit_coverage import active_evidence_set, build_diff_manifest, new_coverage_ledger, update_coverage
from test_batch_contract_handoff import _scope, _dependency, _reported_summary
from test_large_unit_worksets import WorksetProvider, large_file, review_unit
from test_unit_review_summary import _state


class RecoveryProvider(WorksetProvider):
    async def review_step(self, pr, files, diff, model, record_input):
        response = await self.review_unit(pr, files, diff, model, record_input)
        supporting = [item["id"] for item in record_input["evidence"]]
        dependency = _dependency().model_copy(update={"status": "verified", "evidence_ids": supporting})
        return UnitReviewResponse(review_record=response.review_record.model_copy(update={"contract_dependencies": [dependency]}))


def fixture():
    current = [large_file(count=2, width=16).model_copy(update={"file_path": "tests.py"})]
    prior = large_file(count=3, width=16)
    unit = review_unit().model_copy(update={"primary_files": ["tests.py"], "related_files": ["a.py"]})
    manifest = build_diff_manifest(unit, [*current, prior], "head-a", "")
    active = active_evidence_set(manifest, current, "batch-2")
    initial = _reported_summary(current, manifest, active, None)
    request = build_record_input(current, [], None, "head-a", "")
    request["diff_manifest"] = manifest.model_dump(mode="json")
    summary = validate_record(initial.record.model_copy(update={"contract_dependencies": [_dependency()]}),
        request, {"tests.py", "a.py"})
    ledger = update_coverage(new_coverage_ledger(manifest), manifest, active, summary)
    parent = {**_state(), "head_sha": "head-a", "unit_diff_batch": {"id": "batch-2"},
        "batch_parent_diff_files": [prior.model_dump(mode="json")],
        "batch_prior_active_evidence_ids": [item.evidence_id for item in active_evidence_set(manifest, [prior], "batch-1").hunks],
        "unit_metadata": unit.model_dump(mode="json")}
    state = {"unit": unit, "unit_files": current, "unit_diff": "", "unit_plan": None,
        "scope": _scope({"tests.py"}, {"tests.py", "a.py"}), "parent_state": parent,
        "diff_manifest": manifest, "coverage_ledger": ledger, "active_evidence_set": active,
        "review_summary": summary, "last_valid_review_summary": summary,
        "pending_issues": [], "issues": [], "messages": [], "tool_events": [], "context": [],
        "retrieval_history": [], "retrieval_no_new_rounds": 0, "next_action": None,
        "budget": ExecutionBudget(max_model_calls=4, max_diagnosis_attempts=3, max_token_usage=100000),
        "model_usages": [], "issue_round_completed": False}
    return ReviewUnitExecutor(RecoveryProvider(), concurrency=1, timeout_seconds=10), state


@pytest.mark.asyncio
async def test_late_dependency_without_next_need_reads_bound_diff_then_verifies():
    runner, state = fixture()
    state.update(await runner._collect_issue_node(state))
    assert state["next_action"].action == "file_read_diff"
    state.update(await runner._execute_read_tool_node(state))
    assert state["context"] and all(item["source"] == "batch_diff_support" for item in state["context"])
    assert state["budget"].context_retrievals == 1
    assert state["parent_state"]["batch_prior_evidence_bindings"]
    state.update(await runner._report_issue_node(state))
    assert state["review_summary"].record is not None, state["review_summary"].reason
    assert state["review_summary"].record.contract_dependencies[0].status == "verified"
    state.update(await runner._collect_issue_node(state))
    assert state["issue_round_completed"] and state["next_action"] is None
    assert state["budget"].diagnosis_attempts == 1 and state["budget"].model_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["budget", "unavailable", "limit", "no_progress"])
async def test_recovery_failure_preserves_unresolved_dependency(failure):
    runner, state = fixture()
    if failure == "budget":
        state["budget"] = state["budget"].model_copy(update={"max_model_calls": 0})
    elif failure == "unavailable":
        state["parent_state"]["batch_prior_active_evidence_ids"] = []
    else:
        from app.services.unit_coverage import contract_dependency_key
        state["contract_recovery_attempts"] = {contract_dependency_key(_dependency()): 2 if failure == "limit" else 1}
    state.update(await runner._collect_issue_node(state))
    assert state["next_action"] is None and state["issue_round_completed"]
    assert state["review_summary"].record.contract_dependencies[0].status == "unresolved"
    assert "contract_recovery_" in state["messages"][-1].message


@pytest.mark.asyncio
async def test_explicit_next_need_takes_precedence_over_automatic_recovery():
    runner, state = fixture()
    action = AgentAction(action="file_find", reason="查找测试", tool_args={"request": {"query": "test"}})
    state["next_action"] = action
    result = await runner._collect_issue_node(state)
    assert result["next_action"] == action and "contract_recovery_attempts" not in result
