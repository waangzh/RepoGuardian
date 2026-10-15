import pytest

from app.models.review import AgentAction, ExecutionBudget, ReviewUnit, UnitReviewResponse
from app.services.review_unit_executor import ReviewUnitExecutor
from test_review_step_flow_repairs import StepProvider
from test_unit_review_summary import _state


class FileSequenceProvider(StepProvider):
    async def review_step(self, pr, files, diff, model, record_input):
        response = await super().review_step(pr, files, diff, model, record_input)
        index = len(self.inputs) - 1
        need = (AgentAction(action="file_read", reason="核验文件发现机制", tool_args={"request": {
            "file_path": ["pyproject.toml", "setup.py"][index], "start_line": 1, "end_line": 200}})
            if index < 2 else None)
        return UnitReviewResponse(review_record=response.review_record, next_need=need)


@pytest.mark.asyncio
@pytest.mark.parametrize("calls", [2, 3])
async def test_native_followup_reads_two_files_and_rechecks_with_actual_budget(tmp_path, calls):
    for path in ["a.py", "pyproject.toml", "setup.py"]:
        (tmp_path / path).write_text("# " + path + "\n", encoding="utf-8")
    provider = FileSequenceProvider()
    runner = ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=10)
    state = _state()
    state.update(repo_path=str(tmp_path), file_index=[{"path": path} for path in
        ["a.py", "pyproject.toml", "setup.py"]], cross_unit_followup={
            "question": "忽略规则是否改变打包文件发现", "counterevidence_goal": "核验真实发现机制",
            "stop_condition": "证据核验或预算耗尽"}, _cross_unit_budget=ExecutionBudget(
                max_model_calls=calls, max_diagnosis_attempts=calls, max_token_usage=100000,
                max_context_retrievals=2).model_dump(mode="json"))
    unit = ReviewUnit(id="followup-u", primary_files=["a.py"], related_files=["pyproject.toml", "setup.py"],
        fingerprint="f", estimated_tokens=10, complexity="small", grouping_reason="cross_unit_followup")
    result = await runner.execute_unit(unit, state)
    assert len(provider.inputs) == calls, result.error
    assert result.execution_budget.model_calls == calls
    assert result.execution_budget.context_retrievals == 2
    assert any(item["file_path"] == "pyproject.toml" for item in provider.inputs[1]["evidence"])
    if calls == 3:
        assert result.error is None
        assert any(item["file_path"] == "setup.py" for item in provider.inputs[2]["evidence"])
    else:
        assert result.terminal_reason == "diagnosis_budget_exhausted"
        assert "unit_diagnosis_budget_exhausted" in result.error


class ConfigDependencyProvider(StepProvider):
    async def review_step(self, pr, files, diff, model, record_input):
        response = await super().review_step(pr, files, diff, model, record_input)
        dependencies = []
        for path in ["pyproject.toml", "setup.py"]:
            evidence = [item["id"] for item in record_input["evidence"] if item["file_path"] == path]
            dependencies.append({"file_path": path, "symbol": None,
                "assumption": "核实忽略规则是否参与该文件定义的文件发现机制",
                "status": "verified" if evidence else "unresolved", "evidence_ids": evidence})
        from app.models.review import UnitContractDependency
        return UnitReviewResponse(review_record=response.review_record.model_copy(update={
            "contract_dependencies": [UnitContractDependency.model_validate(item) for item in dependencies]}))


@pytest.mark.asyncio
async def test_unbatched_ignore_unit_recovers_two_file_level_dependencies(tmp_path):
    provider = ConfigDependencyProvider()
    runner = ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=10)
    runner._budget_for = lambda *args, **kwargs: ExecutionBudget(
        max_model_calls=5, max_diagnosis_attempts=5, max_token_usage=100000)
    state = _state()
    state["changed_files"][0]["file_path"] = ".gitignore"
    paths = [".gitignore", "pyproject.toml", "setup.py"]
    for path in paths:
        (tmp_path / path).write_text("# " + path + "\n", encoding="utf-8")
    state.update(repo_path=str(tmp_path), file_index=[{"path": path} for path in paths])
    unit = ReviewUnit(id="ignore", primary_files=[".gitignore"], related_files=paths[1:],
        fingerprint="ignore-fp", estimated_tokens=10, complexity="small", grouping_reason="single")
    result = await runner.execute_unit(unit, state)
    assert result.diff_batches == []
    assert len(provider.inputs) == 3, result.error
    assert result.execution_budget.context_retrievals == 2
    assert all(item.status == "verified" for item in result.review_summary.record.contract_dependencies)
    assert result.unit_completion_status == "complete"


def test_file_dependency_recovery_never_expands_authorized_scope():
    from test_late_contract_recovery import fixture
    from test_batch_contract_handoff import _scope
    runner, state = fixture()
    state["parent_state"].pop("unit_diff_batch")
    dependency = state["review_summary"].record.contract_dependencies[0].model_copy(update={"symbol": None})
    state["review_summary"].record.contract_dependencies = [dependency]
    state["scope"] = _scope({"tests.py"}, {"tests.py"})
    assert runner._contract_recovery_action(state)[0] is None
