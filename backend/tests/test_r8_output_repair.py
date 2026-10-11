import asyncio
import json

import pytest
from langchain_core.messages import AIMessage

from app.agents.providers import OpenAICompatibleProvider
from app.models.review import ExecutionBudget, ReviewIssueInput, UnitReviewRecord
from app.review.unit_completion import is_review_unit_complete
from app.services.model_request_budgeter import ModelRequestProfile
from app.services.review_unit_executor import ReviewUnitExecutor
from app.services.unit_review_summary import validate_record
from test_unit_review_summary import _state, _input
from test_issue_validation import _unit


class JsonProvider(OpenAICompatibleProvider):
    def __init__(self, failure="next_need", usage=True):
        super().__init__("fixture", "https://example.test", "fixture",
            request_profile=ModelRequestProfile(context_window=100_000))
        self.inputs, self.prompts = [], []
        self.failure, self.known_usage = failure, usage
        self.repair_started = asyncio.Event()

    def prepare_review_step_request(self, *args):
        self.inputs.append(args[-1])
        return super().prepare_review_step_request(*args)

    def _build_chat_model(self, *args):
        provider = self
        class Chat:
            async def ainvoke(self, messages):
                provider.prompts.append(messages[-1].content)
                request = provider.inputs[-1]
                repaired = bool(request.get("output_repair"))
                if repaired and provider.failure == "cancel":
                    provider.repair_started.set()
                    await asyncio.Event().wait()
                if repaired and provider.failure == "transport":
                    raise RuntimeError("offline transport failure")
                evidence = request["evidence"][0]["id"]
                record = {"change_summary": "检查当前证据", "target_checks": [{
                    "target": item["id"], "status": "checked", "reason": "核验当前证据", "evidence_ids": [evidence]}
                    for item in request["target_catalog"]]}
                issue = ReviewIssueInput(title="候选", category="correctness", severity="high", confidence=.8,
                    affected_behavior="返回值为空", failure_scenario="调用方读取空值", recommendation="核验返回值",
                    primary_evidence={"file_path": "a.py", "existing_code": "return value"}).model_dump(mode="json")
                result = {"issues": [issue], "review_record": record, "next_need": None}
                if not repaired or provider.failure == "twice":
                    if provider.failure in {"next_need", "twice", "cancel", "transport"}:
                        result["next_need"] = {"action": "invalid-action", "reason": "继续"}
                    elif provider.failure == "status":
                        record["target_checks"][0]["status"] = "refuted"
                    elif provider.failure == "replacement":
                        record["question_updates"] = [{"question_id": "old", "status": "superseded", "reason": "替换", "replacement_id": None}]
                    elif provider.failure == "semantic":
                        record["target_checks"][0]["evidence_ids"] = ["unknown-evidence"]
                    elif provider.failure == "target":
                        record["target_checks"][0]["target"] = "改写后的目标"
                content = '{"issues": [}' if not repaired and provider.failure == "json" else json.dumps(result, ensure_ascii=False)
                return AIMessage(content=content, usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
                    if provider.known_usage else None)
        return Chat()


def runner(provider, diagnoses=5):
    result = ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=10)
    result._budget_for = lambda *args, **kwargs: ExecutionBudget(max_model_calls=20, max_diagnosis_attempts=diagnoses,
        max_token_usage=1_000_000, max_context_retrievals=12)
    return result


@pytest.mark.parametrize("failure", ["next_need", "status", "replacement", "json", "target"])
async def test_single_corrective_call_keeps_candidates_and_counts_both_usages(failure):
    provider = JsonProvider(failure)
    result = await runner(provider).execute_unit(_unit(path="a.py"), _state())
    assert len(provider.prompts) == 2, result.error
    assert result.execution_budget.model_calls == 2 and result.execution_budget.diagnosis_attempts == 2
    assert len(result.model_usages) == 2 and sum(item.actual_total_tokens for item in result.model_usages) == 30
    assert result.model_usages[-1].operation == "review_step_repair"
    assert is_review_unit_complete(result), result.review_summary
    assert len(result.issues) == (0 if failure == "json" else 1)  # 纠错不得新增或重复候选。


@pytest.mark.parametrize("failure", ["twice", "transport"])
async def test_bad_or_failed_repair_is_bounded_and_retains_first_candidate(failure):
    provider = JsonProvider(failure)
    result = await runner(provider).execute_unit(_unit(path="a.py"), _state())
    assert len(provider.prompts) == 2 and len(result.issues) == 1
    assert not is_review_unit_complete(result)
    assert result.review_summary.latest_attempt_status == "invalid"


@pytest.mark.parametrize("failure,usage,diagnoses", [("semantic", True, 5), ("next_need", False, 5), ("next_need", True, 1)])
async def test_missing_evidence_unknown_usage_or_no_budget_does_not_retry(failure, usage, diagnoses):
    provider = JsonProvider(failure, usage)
    result = await runner(provider, diagnoses).execute_unit(_unit(path="a.py"), _state())
    assert len(provider.prompts) == 1 and len(result.issues) == 1
    assert not is_review_unit_complete(result)


async def test_corrective_cancellation_propagates():
    provider = JsonProvider("cancel")
    task = asyncio.create_task(runner(provider).execute_unit(_unit(path="a.py"), _state()))
    await asyncio.wait_for(provider.repair_started.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(provider.prompts) == 2


def test_stable_target_id_is_normalized_and_alias_duplicates_are_rejected():
    request = _input()
    from app.services.unit_review_summary import review_target_catalog
    request["target_catalog"] = review_target_catalog(request["targets"])
    target = request["target_catalog"][0]
    check = {"target": target["id"], "status": "checked", "reason": "检查", "evidence_ids": [request["evidence"][0]["id"]]}
    record = UnitReviewRecord(change_summary="检查", target_checks=[check])
    assert validate_record(record, request, {"a.py"}).record.target_checks[0].target == target["target"]
    record = UnitReviewRecord(change_summary="检查", target_checks=[check, {**check, "target": target["target"]}])
    assert validate_record(record, request, {"a.py"}).latest_attempt_status == "invalid"


@pytest.mark.parametrize("max_calls", [1, 200])
async def test_corrective_call_uses_independent_shared_budget_settlement(max_calls):
    from app.services.review_run_budget import ReviewRunBudget, activate_run_budget
    budget = ReviewRunBudget("output-repair", max_calls=max_calls, max_tokens=1_000_000)
    provider = JsonProvider()
    with activate_run_budget(budget):
        result = await runner(provider).execute_unit(_unit(path="a.py"), _state())
    expected = 1 if max_calls == 1 else 2
    assert len(provider.prompts) == expected and budget.calls_consumed == expected
    assert budget.settled_actual == expected * 15
    assert budget.in_flight_reserved == 0 and budget.unknown_reserved == 0
    assert sum(usage.actual_total_tokens for usage in result.model_usages) == expected * 15
    assert is_review_unit_complete(result) == (max_calls != 1)


async def test_recovery_keeps_candidate_and_does_not_reset_corrective_limit():
    provider = JsonProvider("twice")
    executor = runner(provider)
    value = _state()
    first = await executor.execute_unit(_unit(path="a.py"), value)
    assert first.output_repair_attempted and len(first.issues) == 1
    value["review_unit_results"] = [first.model_dump(mode="json")]
    second = await executor.execute_unit(_unit(path="a.py"), value)
    assert len(provider.prompts) == 3 and second.output_repair_attempted
    assert [issue.id for issue in second.issues] == [first.issues[0].id]
    assert len(second.issues[0].source_issue_ids) == 2


async def test_corrective_token_preflight_does_not_consume_an_unsent_attempt():
    baseline = await runner(JsonProvider("semantic")).execute_unit(_unit(path="a.py"), _state())
    tokens = baseline.model_usages[0].accounted_tokens_estimate + 15
    provider = JsonProvider()
    executor = runner(provider)
    executor._budget_for = lambda *args, **kwargs: ExecutionBudget(max_model_calls=2, max_diagnosis_attempts=2,
        max_token_usage=tokens, max_context_retrievals=12)
    result = await executor.execute_unit(_unit(path="a.py"), _state())
    assert len(provider.prompts) == 1 and len(result.model_usages) == 1
    assert result.execution_budget.model_calls == result.execution_budget.diagnosis_attempts == 1
    assert not result.output_repair_attempted


async def test_budget_race_rejection_does_not_persist_a_corrective_attempt():
    from app.agents.providers import LLMProviderError
    provider = JsonProvider()
    executor = runner(provider)
    original = executor._call_unit_model
    async def reject_correction(budget, payload, output_tokens, invoke, **kwargs):
        if payload.get("output_repair"):
            error = LLMProviderError("budget rejected after concurrent settlement")
            error.execution_budget = budget
            error.unit_budget_rejection = {"reason": "budget_calls_insufficient"}
            raise error
        return await original(budget, payload, output_tokens, invoke, **kwargs)
    executor._call_unit_model = reject_correction
    result = await executor.execute_unit(_unit(path="a.py"), _state())
    assert len(provider.prompts) == 1 and len(result.issues) == 1
    assert result.execution_budget.model_calls == result.execution_budget.diagnosis_attempts == 1
    assert not result.output_repair_attempted


async def test_identical_candidates_in_one_response_merge_sources():
    from app.models.review import UnitReviewResponse
    from test_r7_progress_usage_repairs import ProgressProvider
    from app.models.review import AgentAction
    class Repeated(ProgressProvider):
        async def review_step(self, *args):
            value = await super().review_step(*args)
            return UnitReviewResponse(review_record=value.review_record,
                issues=[value.issues[0], value.issues[0].model_copy(update={"id": "another"}, deep=True)])
    provider = Repeated(AgentAction(action="task_done", reason="完成"))
    result = await runner(provider).execute_unit(_unit(path="a.py"), _state())
    assert len(result.issues) == 1 and "another" in result.issues[0].source_issue_ids
