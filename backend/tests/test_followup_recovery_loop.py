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
