from types import SimpleNamespace

import pytest

from app.models.review import AgentAction, ExecutionBudget, UnitReviewRecord, UnitReviewResponse
from app.services.review_unit_executor import ReviewUnitExecutor
from test_unit_review_summary import _state
from app.models.review import ReviewUnit
from app.agents.providers import LLMProvider


class StepProvider(LLMProvider):
    supports_review_step = True
    unit_input_protocol = "canonical-evidence-v3"

    def __init__(self):
        self.inputs = []

    async def decide(self, *args):
        raise AssertionError("review_step must not call a decision model")

    async def review(self, *args):
        raise AssertionError("review_step must not use legacy review")

    async def generate_patch(self, *args):
        return []

    async def review_step(self, pr, files, diff, model, record_input):
        self.inputs.append(record_input)
        record = UnitReviewRecord(change_summary="检查", target_checks=[{
            "target": target, "status": "checked", "reason": "核对证据",
            "evidence_ids": [item["id"] for item in record_input["evidence"]],
        } for target in record_input["targets"]])
        return UnitReviewResponse(review_record=record, next_need=AgentAction(
            action="file_read", reason="补读实现", tool_args={"request": {
                "file_path": "a.py", "start_line": 1, "end_line": 4,
            }}) if len(self.inputs) == 1 else None)


async def test_next_need_executes_tool_and_rechecks_without_decision(tmp_path):
    provider = StepProvider()
    runner = ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=10)
    runner._budget_for = lambda *args, **kwargs: ExecutionBudget(
        max_model_calls=10, max_diagnosis_attempts=5, max_token_usage=100000)
    state = _state()
    (tmp_path / "a.py").write_text("value = 1\n", encoding="utf-8")
    state.update(repo_path=str(tmp_path), file_index=[{"path": "a.py"}, {"path": "b.py"}])
    unit = ReviewUnit(id="u1", primary_files=["a.py"], fingerprint="f", estimated_tokens=10,
                      complexity="small", grouping_reason="single")
    result = await runner.execute_unit(unit, state)
    assert len(provider.inputs) == 2, result.error
    assert any(event.tool == "file_read" and event.status == "completed" for event in result.tool_events)
    assert provider.inputs[1]["evidence"]


async def test_unsupported_next_need_is_rejected_before_routing():
    runner = ReviewUnitExecutor(StepProvider(), concurrency=1, timeout_seconds=5)
    state = {"pending_issues": [], "issues": [], "unit": SimpleNamespace(id="u"),
             "scope": SimpleNamespace(), "messages": [],
             "next_action": AgentAction(action="finish_report", reason="越界动作")}
    runner._filter_issues = lambda *args: []
    result = await runner._collect_issue_node(state)
    assert result["error"] == "review_step_next_need_not_allowed"
    assert result["next_action"].action.value == "task_done"


@pytest.mark.parametrize("action", ["file_read", "file_find", "code_search", "file_read_diff"])
async def test_collect_preserves_controlled_need(action):
    requests = {
        "file_read": {"file_path": "a.py", "start_line": 1, "end_line": 2},
        "file_find": {"query": "a.py"}, "code_search": {"query": "foo", "relation": "text"},
        "file_read_diff": {"file_path": "a.py"},
    }
    runner = ReviewUnitExecutor(StepProvider(), concurrency=1, timeout_seconds=5)
    need = AgentAction(action=action, reason="取证", tool_args={"request": requests[action]})
    state = {"pending_issues": [], "issues": [], "unit": SimpleNamespace(id="u"),
             "scope": SimpleNamespace(), "messages": [], "next_action": need}
    runner._filter_issues = lambda *args: []
    result = await runner._collect_issue_node(state)
    assert result["next_action"] == need
    assert not result["issue_round_completed"]
