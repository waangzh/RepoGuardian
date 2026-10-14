import pytest

from app.agents.providers import LLMProviderError, OpenAICompatibleProvider
from app.evaluation.runner import ObservedProvider
from app.models.review import (AgentAction, ExecutionBudget, IssueStatus, ModelCallResult, ModelUsage, ReviewUnitToolEvent,
    UnitReviewRecord, UnitReviewResponse)
from app.review.unit_completion import is_review_unit_complete, review_unit_coverage_warning
from app.services.model_request_budgeter import ModelRequestProfile
from app.services.review_unit_executor import ReviewUnitExecutor
from test_unit_review_summary import _file, _state
from test_issue_validation import _unit, _issue


class ProgressProvider(OpenAICompatibleProvider):
    def __init__(self, action):
        super().__init__("fixture", "https://example.test", "fixture",
                         request_profile=ModelRequestProfile(context_window=100_000))
        self.inputs = []
        self.action = action

    async def review_step(self, pr, files, diff, model, record_input):
        self.inputs.append(record_input)
        record = UnitReviewRecord(change_summary="检查返回值", target_checks=[{
            "target": target, "status": "checked", "reason": "已检查", "evidence_ids": [record_input["evidence"][0]["id"]]
        } for target in record_input["targets"]])
        issue = _issue(f"candidate-{len(self.inputs)}", path="a.py", status="candidate")
        return UnitReviewResponse(review_record=record, issues=[issue], next_need=self.action)


@pytest.mark.parametrize("action", [AgentAction(action="report_issue", reason="继续报告"),
    AgentAction(action="file_read_diff", reason="取证", tool_args={"request": {"file_path": "outside.py"}})])
async def test_native_repeated_report_or_rejected_read_is_bounded_and_retains_candidate(action):
    provider = ProgressProvider(action)
    runner = ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=10)
    runner._budget_for = lambda *args, **kwargs: ExecutionBudget(max_model_calls=200, max_diagnosis_attempts=200,
        max_token_usage=1_000_000, max_context_retrievals=12)
    result = await runner.execute_unit(_unit(path="a.py"), _state())
    assert len(provider.inputs) == 2, result.error
    assert result.issues
    if action.action.value == "report_issue":
        assert is_review_unit_complete(result)
        assert result.review_summary.latest_attempt_status == "reported"
    else:
        assert result.review_summary.latest_attempt_reason == "review_step_no_evidence_progress"
        assert not is_review_unit_complete(result)
    assert all(issue.status == IssueStatus.candidate for issue in result.issues)
    if action.action.value == "file_read_diff":
        assert provider.inputs[1]["tool_feedback"][-1]["status"] == "rejected"
        assert "a.py" in provider.inputs[1]["readonly_tool_scope"]["file_read_diff"]
        assert len(result.tool_events) == 2


async def test_new_evidence_can_continue_after_old_evidence_recheck_limit():
    provider = ProgressProvider(AgentAction(action="report_issue", reason="继续"))
    runner = ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=10)
    from app.models.review import ExecutionBudget
    state = {"parent_state": _state(), "unit": _unit(path="a.py"), "unit_files": [_file()],
             "context": [], "messages": [], "issue_round_completed": False, "retrieval_no_new_rounds": 0,
             "budget": ExecutionBudget(), "next_action": None}
    old = runner._native_evidence_key(state)
    state["native_review_counts"] = {old: 2}
    state["context"] = [{"file": "a.py", "start_line": 1, "end_line": 1, "source": "file_read", "content": "new evidence"}]
    decision = await runner._agent_decide_node(state)
    assert decision["next_action"].action.value == "report_issue"
    assert "terminal_reason" not in decision


async def test_new_file_find_facts_allow_review_but_repeated_facts_do_not():
    provider = ProgressProvider(AgentAction(action="report_issue", reason="继续"))
    runner = ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=10)
    state = {"parent_state": _state(), "unit": _unit(path="a.py"), "unit_files": [_file()],
             "context": [], "messages": [], "issue_round_completed": False, "retrieval_no_new_rounds": 0,
             "next_action": None}
    state["native_review_counts"] = {runner._native_evidence_key(state): 2}
    event = ReviewUnitToolEvent(review_unit_id=state["unit"].id, tool="file_find", status="completed",
                               result_count=1, detail='{"files": ["caller.py"]}')
    state["tool_events"] = [event]
    decision = await runner._agent_decide_node(state)
    assert decision["next_action"].action.value == "report_issue"
    state["native_review_counts"][runner._native_evidence_key(state)] = 2
    state["tool_events"].append(event.model_copy())
    decision = await runner._agent_decide_node(state)
    assert decision["latest_review_attempt"]["reason"] == "review_step_no_evidence_progress"


async def test_coverage_gate_does_not_restart_review_stopped_for_no_progress():
    class MissingChecksProvider(ProgressProvider):
        async def review_step(self, *args):
            result = await super().review_step(*args)
            return result.model_copy(update={"review_record": UnitReviewRecord(change_summary="未提供检查")})

    provider = MissingChecksProvider(AgentAction(action="report_issue", reason="继续"))
    runner = ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=10)
    runner._budget_for = lambda *args, **kwargs: ExecutionBudget(max_model_calls=200, max_diagnosis_attempts=200,
        max_token_usage=1_000_000, max_context_retrievals=12)
    result = await runner.execute_unit(_unit(path="a.py"), _state())
    assert result.diff_manifest is not None and result.coverage_ledger is not None
    assert len(provider.inputs) == 2 and result.error is None
    assert result.terminal_reason.value == "no_new_context"
    assert result.review_summary.latest_attempt_reason == "review_step_no_evidence_progress"
    assert not is_review_unit_complete(result) and result.issues


@pytest.mark.parametrize("repeat_tail_only", [False, True])
async def test_equivalent_file_find_queries_and_thirteenth_path_are_not_new_progress(monkeypatch, repeat_tail_only):
    from app.tools.context_files import ScopedContextTool

    paths = [f"caller{i}.py" for i in range(13)]
    async def find_page(self, **kwargs):
        return {"files": paths[-1:] if repeat_tail_only and kwargs["query"] != "caller" else paths,
                "next_cursor": None, "truncated": False}
    monkeypatch.setattr(ScopedContextTool, "file_find_page", find_page)

    class FindProvider(ProgressProvider):
        async def review_step(self, *args):
            self.action = AgentAction(action="file_find", reason="发现调用方", tool_args={"request": {
                "query": ["caller", "CALLER", "caller.py"][min(len(self.inputs), 2)], "max_results": 20}})
            return await super().review_step(*args)

    provider = FindProvider(None)
    runner = ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=10)
    original_scope = runner.planner.build_scope
    runner.planner.build_scope = lambda *args, **kwargs: original_scope(*args, **kwargs).model_copy(update={"max_search_results": 20})
    runner._budget_for = lambda *args, **kwargs: ExecutionBudget(max_model_calls=200, max_diagnosis_attempts=200,
        max_token_usage=1_000_000, max_context_retrievals=12)
    result = await runner.execute_unit(_unit(path="a.py"), _state())
    assert len(provider.inputs) == 3 and len(result.tool_events) == 3, result.error
    assert provider.inputs[1]["tool_feedback"][-1]["files"] == paths
    assert result.terminal_reason.value == "no_new_context" and result.error is None
    assert not is_review_unit_complete(result)


class DuplicateReadProvider(OpenAICompatibleProvider):
    """先成功读取一次，再重复请求同一范围；可注入未决契约制造反例。"""

    def __init__(self, unresolved=None):
        super().__init__("fixture", "https://example.test", "fixture",
                         request_profile=ModelRequestProfile(context_window=100_000))
        self.inputs = []
        self.unresolved = unresolved

    async def review_step(self, pr, files, diff, model, record_input):
        self.inputs.append(record_input)
        evidence = record_input["evidence"][0]["id"]
        record = UnitReviewRecord(change_summary="检查返回值", target_checks=[{
            "target": target, "status": "checked", "reason": "已检查", "evidence_ids": [evidence]
        } for target in record_input["targets"]])
        if self.unresolved:
            record = UnitReviewRecord.model_validate({**record.model_dump(), "unresolved_questions": [{
                "question": self.unresolved, "affected_files": ["a.py"], "evidence_ids": [evidence]}]})
        return UnitReviewResponse(review_record=record, next_need=AgentAction(
            action="file_read", reason="重复请求已展示的正文",
            tool_args={"request": {"file_path": "a.py", "start_line": 1, "end_line": 2}}))


def duplicate_read_runner(provider):
    runner = ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=10)
    runner._budget_for = lambda *args, **kwargs: ExecutionBudget(max_model_calls=200, max_diagnosis_attempts=200,
        max_token_usage=1_000_000, max_context_retrievals=12)
    return runner


async def test_duplicate_read_after_closed_review_finishes_normally(monkeypatch):
    from app.tools.context_files import ScopedContextTool

    async def read(*args, **kwargs):
        return {"file": "a.py", "start_line": 1, "end_line": 2, "content": "line one\nline two",
                "requested_end_line": 2, "truncated": False, "relevance": "direct",
                "review_unit_id": kwargs["scope"].review_unit_id, "source": "file_read",
                "distance": 0, "confidence": 1.0, "why_retrieved": "fixture"}
    monkeypatch.setattr(ScopedContextTool, "file_read", read)

    provider = DuplicateReadProvider()
    result = await duplicate_read_runner(provider).execute_unit(_unit(path="a.py"), _state())

    # 模型只被调用两次：一次完整审查，一次重复取证请求被服务端按已满足拒绝。
    assert len(provider.inputs) == 2, result.error
    assert result.review_summary.latest_attempt_status == "reported"
    assert is_review_unit_complete(result)
    assert result.terminal_reason.value == "completed"
    assert [event.detail for event in result.tool_events if event.status == "rejected"] == [
        "duplicate read request already satisfied: requested body is displayed and unchanged"]
    assert review_unit_coverage_warning(result) is None


async def test_duplicate_read_with_unresolved_contract_still_blocks_completion(monkeypatch):
    from app.tools.context_files import ScopedContextTool

    async def read(*args, **kwargs):
        return {"file": "a.py", "start_line": 1, "end_line": 2, "content": "line one\nline two",
                "requested_end_line": 2, "truncated": False, "relevance": "direct",
                "review_unit_id": kwargs["scope"].review_unit_id, "source": "file_read",
                "distance": 0, "confidence": 1.0, "why_retrieved": "fixture"}
    monkeypatch.setattr(ScopedContextTool, "file_read", read)

    provider = DuplicateReadProvider(unresolved="调用方是否处理空值？")
    result = await duplicate_read_runner(provider).execute_unit(_unit(path="a.py"), _state())

    # 未决契约存在时重复请求不满足，两轮拒绝后仍按无进展保护收敛并保留未完成状态。
    assert len(provider.inputs) == 3, result.error
    assert result.review_summary.latest_attempt_status == "unknown"
    assert result.review_summary.latest_attempt_reason == "review_step_no_evidence_progress"
    assert not is_review_unit_complete(result)
    assert [event.detail for event in result.tool_events if event.status == "rejected"] == [
        "duplicate read request", "duplicate read request"]
    warning = review_unit_coverage_warning(result)
    assert warning and "契约" in warning


def test_aggregate_does_not_claim_complete_when_latest_attempt_has_no_record():
    from app.models.review import (UnitDiffBatch, UnitInputCoverage, UnitReviewRecord,
        UnitReviewSummary, ReviewUnitResult)
    from test_large_unit_worksets import review_unit

    record = UnitReviewRecord(change_summary="检查本批范围")
    batch_result = ReviewUnitResult(review_unit_id="batch-1", status="completed",
        review_summary=UnitReviewSummary(status="reported", record=record, last_valid_record=record,
            latest_attempt_status="unknown", latest_attempt_reason="review_step_no_evidence_progress"),
        input_coverage=UnitInputCoverage(target_coverage="complete", contract_coverage="complete"))
    batch = UnitDiffBatch(id="batch-1", parent_unit_id="large", input_hash="batch-1",
        version="unit-diff-worksets-v1", ranges=[], admission={}, status="completed", result=batch_result)

    result = ReviewUnitExecutor._aggregate_diff_batches(review_unit(), [batch], ExecutionBudget(), "binding")

    # 覆盖字段完整但最近一次尝试没有可用记录时，聚合不能与严格完成相矛盾。
    assert result.input_coverage.target_coverage == "complete"
    assert result.unit_completion_status == "incomplete"
    assert not is_review_unit_complete(result)
    warning = review_unit_coverage_warning(result)
    assert warning and "审查记录不可用" in warning and "large:unknown" in warning


@pytest.mark.parametrize("method", ["review_step", "summarize_pr_purpose", "verify_issue", "deduplicate_issues", "coordinate_cross_units"])
async def test_completed_usage_survives_invalid_json_for_all_response_adapters(method):
    provider = OpenAICompatibleProvider("fixture", "https://example.test", "fixture")
    usage = ModelUsage(provider="fixture", model="fixture", operation="review_step",
                       actual_input_tokens=21_178, actual_output_tokens=2_895, actual_total_tokens=24_073, latency_ms=1)
    async def response(*args, **kwargs):
        return ModelCallResult('{"issues": [}', usage)
    provider._send_prepared_json_request = response
    provider._request_json_content = response
    provider.prepare_review_step_request = lambda *args: None
    provider._context_prompt = lambda *args: "fixture"
    provider._build_coordination_prompt = lambda *args: "fixture"
    from app.models.review import PullRequestInfo
    pr = PullRequestInfo.model_validate(_state()["pr_info"])
    args = {"review_step": (pr, [], "", None, {}), "summarize_pr_purpose": (pr, [], None),
            "verify_issue": (type("Request", (), {"budget": type("Budget", (), {"max_output_tokens": 100})()})(), None),
            "deduplicate_issues": ([], None), "coordinate_cross_units": ({}, None)}[method]
    observed = ObservedProvider(provider)
    with pytest.raises(LLMProviderError) as caught:
        await getattr(observed, method)(*args)
    assert caught.value.usage.id == usage.id
    assert observed.calls[0]["usage"]["actual_total_tokens"] == 24_073
    assert "{\"issues\"" not in str(caught.value)
