import pytest

from app.models.review import AgentAction, UnitQuestionCoverage
from app.services.unit_coverage import contract_dependency_key
from test_late_contract_recovery import fixture
from test_batch_contract_handoff import _dependency


@pytest.mark.asyncio
async def test_recovery_locates_missing_symbol_instead_of_unrelated_same_file_context():
    runner, state = fixture()
    # 同文件已有片段，但不覆盖依赖涉及的符号定义。
    state["context"] = [{"file": "a.py", "start_line": 1, "end_line": 40,
                         "content": "unrelated", "source": "file_read", "relevance": "direct"}]
    state["parent_state"]["symbol_index"] = [
        {"file": "a.py", "symbol": "weighted_point", "type": "function",
         "start_line": 360, "end_line": 380}]

    state.update(await runner._collect_issue_node(state))

    assert state["next_action"].action == "file_read"
    assert state["next_action"].tool_args["request"] == {
        "file_path": "a.py", "start_line": 360, "end_line": 380}
    assert state["recovery_focus"]["target_key"] == contract_dependency_key(_dependency())
    # 选定补审不等于执行完成：此时不计次。
    assert "contract_recovery_attempts" not in state


@pytest.mark.asyncio
async def test_recovery_settles_attempt_only_after_execution_and_clears_focus():
    runner, state = fixture()
    state.update(await runner._collect_issue_node(state))
    assert state["next_action"].action == "file_read_diff" and state["recovery_focus"]
    state.update(await runner._execute_read_tool_node(state))
    # 取证动作尚未完成定向复核，计次仍不结算。
    assert "contract_recovery_attempts" not in state and state["recovery_focus"]

    state.update(await runner._report_issue_node(state))

    assert state["contract_recovery_attempts"] == {contract_dependency_key(_dependency()): 1}
    assert state.get("recovery_focus") is None
    # 定向复核输入必须显式声明本轮焦点。
    assert runner.provider.requests[-1][1]["recovery_focus"]["kind"] == "dependency"


@pytest.mark.asyncio
async def test_selected_recovery_is_not_truncated_by_no_progress_guard():
    runner, state = fixture()
    state.update(next_action=AgentAction(action="report_issue", reason="定向复核"),
                 recovery_focus={"target_key": "k", "kind": "dependency"},
                 native_review_counts={runner._native_evidence_key(state): 5})

    decision = await runner._agent_decide_node(state)

    assert decision["next_action"].action == "report_issue"
    assert "terminal_reason" not in decision and "review_summary" not in decision
    # 无补审焦点时同一保护仍然截断重复报告。
    state.pop("recovery_focus")
    decision = await runner._agent_decide_node(state)
    assert decision["next_action"].action == "task_done"
    assert decision["terminal_reason"] == "no_new_context"


def test_pending_question_gets_one_bounded_targeted_recheck():
    runner, state = fixture()
    state["review_summary"] = state["review_summary"].model_copy(update={
        "record": state["review_summary"].record.model_copy(update={"contract_dependencies": []})})
    state["coverage_ledger"] = state["coverage_ledger"].model_copy(update={"questions": {
        "q-boundary": UnitQuestionCoverage(question="测试是否覆盖 (0,40) 与 (0,3)？",
            affected_files=["tests.py"], evidence_ids=[])}})

    action, key, focus = runner._contract_recovery_action(state)

    assert action.action == "report_issue" and key == "question:q-boundary"
    assert focus == {"target_key": key, "kind": "question", "question_id": "q-boundary",
                     "question": "测试是否覆盖 (0,40) 与 (0,3)？", "affected_files": ["tests.py"]}
    # 每个问题每批只定向复核一次，独立于依赖通道限额。
    state["contract_recovery_attempts"] = {key: 1}
    assert runner._contract_recovery_action(state)[0] is None


@pytest.mark.asyncio
async def test_unbindable_supporting_diff_read_is_rejected_not_silently_empty():
    runner, state = fixture()
    from test_batch_contract_handoff import _scope

    state["scope"] = _scope({"tests.py"}, {"tests.py", "a.py", "c.py"})
    state["parent_state"]["batch_parent_diff_files"] = [
        {"file_path": "c.py", "change_type": "modified", "additions": 0, "deletions": 0, "hunks": []}]
    action = AgentAction(action="file_read_diff", reason="取证",
                         tool_args={"request": {"file_path": "c.py", "hunk_ids": []}})

    result = await runner._execute_direct_read_action(state, action, state["budget"], [], [])

    assert result["tool_events"][-1].status == "rejected"
    assert "citable evidence" in result["tool_events"][-1].detail
    assert result["retrieval_no_new_rounds"] == 1
