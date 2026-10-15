import pytest

from app.models.review import AgentAction, ExecutionBudget, ReviewUnit, UnitHypothesisCheck, UnitReviewResponse
from app.services.review_unit_executor import ReviewUnitExecutor
from test_review_step_flow_repairs import StepProvider
from test_unit_review_summary import _state


class EvidenceProvider(StepProvider):
    async def review_step(self, pr, files, diff, model, record_input):
        response = await super().review_step(pr, files, diff, model, record_input)
        response.review_record.hypothesis_checks = [UnitHypothesisCheck(
            hypothesis_id=item["id"], status="refuted", reason="仅核验已展示正文",
            evidence_ids=[ref["id"] for ref in record_input["evidence"]]) for item in record_input["hypotheses"]]
        return UnitReviewResponse(review_record=response.review_record, next_need=AgentAction(
            action="file_read", reason="复现只读前 200 行", tool_args={"request": {
                "file_path": "pyproject.toml", "start_line": 1, "end_line": 200}})
            if getattr(self, "first_page", False) and len(self.inputs) == 1 else None)


def followup_state(tmp_path=None, retrievals=4):
    value = _state()
    value.update(cross_unit_followup={"question": "文档和配置是否引用本地文件？",
        "counterevidence_goal": "核验限定正文中的显式引用", "stop_condition": "逐文件核验或保留缺口",
        "supporting_files": [], "verification_files": ["a.py", "pyproject.toml", "setup.py"]},
        _cross_unit_budget=ExecutionBudget(max_model_calls=8, max_diagnosis_attempts=8,
            max_token_usage=200000, max_context_retrievals=retrievals).model_dump(mode="json"))
    if tmp_path:
        value["repo_path"] = str(tmp_path)
        value["file_index"] = [{"path": path} for path in ["a.py", "pyproject.toml", "setup.py"]]
        (tmp_path / "a.py").write_text("value = 1\n", encoding="utf-8")
        (tmp_path / "pyproject.toml").write_text("# config\n" * 212 + "# final setting\n", encoding="utf-8")
        (tmp_path / "setup.py").write_text("setup()\n", encoding="utf-8")
    return value


def followup_unit():
    return ReviewUnit(id="followup-evidence", primary_files=["a.py"],
        related_files=["pyproject.toml", "setup.py"], fingerprint="followup-evidence",
        estimated_tokens=10, complexity="small", grouping_reason="cross_unit_followup")


@pytest.mark.asyncio
async def test_required_evidence_is_read_without_model_dependencies_and_reaches_eof(tmp_path):
    provider = EvidenceProvider()
    provider.first_page = True
    result = await ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=10).execute_unit(followup_unit(), followup_state(tmp_path))
    assert result.error is None
    assert result.execution_budget.context_retrievals == 3
    assert len(provider.inputs) == 4
    last = provider.inputs[-1]
    assert any(item["file_path"] == "setup.py" for item in last["evidence"])
    assert any("final setting" in item["content"] for item in last["readonly_context"])
    snippets = [item for item in result.context_snippets if item.file == "pyproject.toml"]
    assert all(item.total_lines == 213 for item in snippets)


@pytest.mark.asyncio
async def test_required_evidence_does_not_bypass_retrieval_budget(tmp_path):
    provider = EvidenceProvider()
    result = await ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=10).execute_unit(followup_unit(), followup_state(tmp_path, 1))
    assert result.execution_budget.context_retrievals == 1
    assert len(provider.inputs) == 2
    assert not any(item["file_path"] == "setup.py" for item in provider.inputs[-1]["evidence"])
    assert any("followup_evidence_budget_exhausted" in (item.message or "") for item in result.messages)


@pytest.mark.asyncio
async def test_supporting_diff_is_delivered_in_first_input_without_tool_read():
    value = followup_state()
    value["changed_files"].append({**value["changed_files"][0], "file_path": "AGENTS.md"})
    value["cross_unit_followup"].update(supporting_files=["AGENTS.md"], verification_files=["a.py", "AGENTS.md"])
    unit = followup_unit().model_copy(update={"related_files": ["AGENTS.md"]})
    provider = EvidenceProvider()
    result = await ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=10).execute_unit(unit, value)
    assert result.error is None
    assert {item["file_path"] for item in provider.inputs[0]["evidence"]} == {"a.py", "AGENTS.md"}
    assert result.execution_budget.context_retrievals == 0
    assert unit.primary_files == ["a.py"]


@pytest.mark.asyncio
async def test_necessary_file_outside_scope_is_not_read(tmp_path):
    value = followup_state(tmp_path)
    value["cross_unit_followup"]["verification_files"] = ["outside.py"]
    provider = EvidenceProvider()
    result = await ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=10).execute_unit(followup_unit(), value)
    assert result.execution_budget.context_retrievals == 0
    assert any("followup_evidence_unavailable:outside.py" in (item.message or "") for item in result.messages)
