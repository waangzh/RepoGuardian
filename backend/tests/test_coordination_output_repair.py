import json

import pytest
from langchain_core.messages import AIMessage

from app.agents.providers import LLMProviderError, OpenAICompatibleProvider
from app.models.review import ExecutionBudget
from app.services.coordination_runtime import CoordinationRuntime
from app.services.cross_unit_coordination import CrossUnitCoordinationService
from test_cross_unit_coordination import plan, state
from test_provider import fake_chat as fake_chat
from test_phase6a_persistence import persistence as persistence


def response(payload, *, decision="uncertain", tokens=6668):
    raw = plan(payload).model_dump(mode="json", include={"decision", "reason", "followups"})
    raw["decision"] = decision
    return AIMessage(content=json.dumps(raw, ensure_ascii=False), usage_metadata={
        "input_tokens": tokens - 100, "output_tokens": 100, "total_tokens": tokens} if tokens is not None else None)


@pytest.mark.asyncio
async def test_provider_conflict_keeps_usage_and_repair_has_distinct_request_and_accounting(fake_chat):
    payload = CrossUnitCoordinationService.catalog(state())
    fake_chat.responses = [response(payload), response(payload, decision="required", tokens=7000)]
    provider = OpenAICompatibleProvider("key", "https://example.test/v1", "model")
    runtime = CoordinationRuntime("task", "fingerprint", ExecutionBudget(max_model_calls=10, max_token_usage=200000))
    with pytest.raises(LLMProviderError) as caught:
        await runtime.call(provider, "coordinate_cross_units", (payload, None), 4096)
    error = caught.value
    assert error.coordination_error_code == "decision_followups_conflict"
    assert error.usage.actual_total_tokens == 6668
    assert error.output_schema_errors[0]["loc"] == ["followups"]
    feedback = {"attempt": 1, "errors": error.output_schema_errors}
    prepared = provider.prepare_coordination_request(payload, None, feedback)
    result = await runtime.call(provider, "repair_cross_unit_plan", (payload, None, feedback), 4096)
    assert result.decision == "required"
    assert result.usage.operation == "cross_unit_coordination_repair"
    assert prepared.prompt == fake_chat.instances[1].messages[1].content
    assert result.usage.response_metadata["request_admission"]["request_hash"] == prepared.estimate(
        provider.resolve_request_profile(None).profile)["request_hash"]
    assert runtime.budget.model_calls == 2
    assert runtime.budget.token_usage == 13668
    assert sorted(item["status"] for item in runtime.data["calls"].values()) == ["completed", "failed"]
    with pytest.raises(LLMProviderError) as restored:
        await runtime.call(provider, "coordinate_cross_units", (payload, None), 4096)
    assert restored.value.output_schema_errors == error.output_schema_errors
    assert restored.value.usage.id == error.usage.id
    await runtime.call(provider, "repair_cross_unit_plan", (payload, None, feedback), 4096)
    assert len(fake_chat.instances) == 2
    assert runtime.budget.token_usage == 13668


def test_prompt_uses_three_mutually_exclusive_examples_without_reinterpreting_risk():
    prompt = OpenAICompatibleProvider._build_coordination_prompt({})
    examples = [json.loads(line) for line in prompt.splitlines() if line.startswith('{"decision"')]
    assert {item["decision"] for item in examples} == {"required", "uncertain", "skip"}
    assert all(not item["followups"] for item in examples if item["decision"] != "required")
    assert next(item["followups"] for item in examples if item["decision"] == "required")
    assert "不表示风险已经确认" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["valid", "again_invalid", "path", "evidence", "question_index",
    "count", "downgrade", "legal_empty", "budget_calls", "budget_tokens", "window", "unknown_usage", "json", "transport"])
async def test_catalog_repair_is_bounded_and_revalidates_every_execution_gate(fake_chat, monkeypatch, scenario):
    from app.models.review import ReviewUnitResult
    from app.services.coordination_catalog import catalog_batches
    from app.services.review_unit_executor import ReviewUnitExecutor
    value = state()
    value["cross_unit_risk"]["decision"] = "required" if scenario == "downgrade" else "uncertain"
    payload = CrossUnitCoordinationService.catalog(value)
    first = response(payload, tokens=None if scenario == "unknown_usage" else 6668)
    second = response(payload, decision="required", tokens=7000)
    raw = json.loads(second.content)
    if scenario == "again_invalid":
        second = response(payload, tokens=7000)
    elif scenario == "path":
        raw["followups"][0]["primary_files"] = ["outside.py"]
    elif scenario == "evidence":
        raw["followups"][0]["evidence_ids"] = ["stale-id"]
    elif scenario == "question_index":
        raw["followups"][0]["planning_question_indices"] = [5]
    elif scenario == "count":
        raw["followups"] = [{**raw["followups"][0], "id": f"f{index}"} for index in range(4)]
    elif scenario in {"legal_empty", "downgrade"}:
        raw.update(decision="uncertain", followups=[], unresolved_questions=["尚缺形成可执行计划的依据"])
    if scenario != "again_invalid":
        second = second.model_copy(update={"content": json.dumps(raw)})
    if scenario == "json":
        first = first.model_copy(update={"content": "not-json"})
    elif scenario == "transport":
        first = RuntimeError("fixture transport failure")
    fake_chat.responses = [first, second]
    provider = OpenAICompatibleProvider("key", "https://example.test/v1", "model", request_attempts=1)
    if scenario == "window":
        from dataclasses import replace
        prepare = provider.prepare_coordination_request

        def oversized(payload, model, feedback=None):
            request = prepare(payload, model, feedback)
            return replace(request, prompt=request.prompt + "context padding " * 5000) if feedback else request

        monkeypatch.setattr(provider, "prepare_coordination_request", oversized)
    tokens = (provider.coordination_request_admission(catalog_batches(payload)[0][1], None)["reserved_tokens"]
              if scenario == "budget_tokens" else 200000)
    service = CrossUnitCoordinationService(provider, budget=ExecutionBudget(
        max_model_calls=1 if scenario == "budget_calls" else 16, max_token_usage=tokens))
    executed = []

    async def execute(self, unit, local):
        executed.append(unit)
        assert local["cross_unit_followup"]["unit_ids"] == ["u0", "u1"]
        return ReviewUnitResult(review_unit_id=unit.id, status="completed")

    monkeypatch.setattr(ReviewUnitExecutor, "execute_unit", execute)
    result = await service.run(value)
    no_repair_send = scenario in {"budget_calls", "budget_tokens", "window", "unknown_usage", "json", "transport"}
    assert len([instance for instance in fake_chat.instances if instance.messages]) == (1 if no_repair_send else 2), result
    assert len(executed) == (1 if scenario == "valid" else 0)
    assert result["coordination_plan"]["status"] == ("unresolved" if scenario in {"valid", "legal_empty"} else "failed"), result
    if scenario == "valid":
        assert result["coordination_plan"]["catalog_batches"][0]["status"] == "validated"
        assert len(result["followup_results"]) == 1
    elif scenario == "legal_empty":
        assert result["coordination_plan"]["decision"] == "uncertain"
        assert result["coordination_plan"]["followups"] == []
    if scenario == "again_invalid":
        assert result["coordination_plan"]["reason"] == "协调计划的决策与补查列表冲突，未执行补查"
    if scenario in {"budget_calls", "budget_tokens", "window"}:
        assert "纠错请求未获预算或窗口准入" in result["coordination_plan"]["reason"]
    if not no_repair_send:
        assert {item["operation"] for item in result["model_usages"]} == {
            "cross_unit_coordination", "cross_unit_coordination_repair"}
        assert sum(item["actual_total_tokens"] for item in result["model_usages"]) == 13668
    assert len(service.runtime.data["calls"]) <= 2
    assert any(item["status"] == "failed" for item in service.runtime.data["calls"].values())


@pytest.mark.asyncio
async def test_failed_response_is_restored_then_repaired_without_rebilling_first_call(fake_chat, persistence):
    import asyncio
    from app.models.review import ReviewTask
    repository = persistence[0]
    value = state()
    value["cross_unit_risk"]["decision"] = "uncertain"
    repository.create_task(ReviewTask(id=value["task_id"], pr_url=value["pr_info"]["html_url"]))
    payload = CrossUnitCoordinationService.catalog(value)
    first = response(payload)
    second = AIMessage(content='{"decision":"uncertain","reason":"还不能形成可执行计划","followups":[],"unresolved_questions":["证据缺口"]}',
        usage_metadata={"input_tokens":6900,"output_tokens":100,"total_tokens":7000})
    fake_chat.responses = [first, second]
    provider = OpenAICompatibleProvider("key", "https://example.test/v1", "model")
    stopped = False
    service = None

    def progress(delta):
        nonlocal stopped
        if not stopped and any(item.get("coordination_error_code") == "decision_followups_conflict"
                               for item in service.runtime.data["calls"].values()):
            stopped = True
            raise asyncio.CancelledError

    service = CrossUnitCoordinationService(provider, repository=repository, progress=progress)
    with pytest.raises(asyncio.CancelledError):
        await service.run(value)
    assert len(fake_chat.instances) == 1
    restarted = CrossUnitCoordinationService(provider, repository=repository)
    result = await restarted.run(value)
    assert result["coordination_plan"]["catalog_batches"][0]["status"] == "validated", result
    assert len(fake_chat.instances) == 2
    assert restarted.runtime.budget.model_calls == 2
    assert restarted.runtime.budget.token_usage == 13668
    replay = await CrossUnitCoordinationService(provider, repository=repository).run(value)
    assert len(fake_chat.instances) == 2
    assert {item["id"] for item in replay["model_usages"]} == {item["id"] for item in result["model_usages"]}


@pytest.mark.asyncio
async def test_unified_run_budget_accounts_both_attempts_once(fake_chat):
    from app.services.review_run_budget import ReviewRunBudget, activate_run_budget
    value = state()
    value["cross_unit_risk"]["decision"] = "uncertain"
    payload = CrossUnitCoordinationService.catalog(value)
    second = response(payload, decision="required", tokens=7000)
    raw = json.loads(second.content)
    raw.update(decision="uncertain", followups=[])
    fake_chat.responses = [response(payload), second.model_copy(update={"content": json.dumps(raw)})]
    provider = OpenAICompatibleProvider("key", "https://example.test/v1", "model")
    budget = ReviewRunBudget("run", max_tokens=200000, max_calls=10)
    with activate_run_budget(budget):
        result = await CrossUnitCoordinationService(provider).run(value)
    assert result["coordination_plan"]["catalog_batches"][0]["status"] == "validated"
    assert budget.calls_consumed == 2
    assert budget.settled_actual == 13668
    assert len(budget.snapshot()["attempts"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("during_repair", [False, True])
async def test_cancellation_preserves_unknown_and_never_sends_an_extra_repair(fake_chat, monkeypatch, during_repair):
    import asyncio
    payload = CrossUnitCoordinationService.catalog(state())
    fake_chat.responses = [response(payload)]
    original = fake_chat.ainvoke
    calls = []

    async def invoke(self, messages):
        calls.append(messages)
        if len(calls) == (2 if during_repair else 1):
            raise asyncio.CancelledError
        return await original(self, messages)

    monkeypatch.setattr(fake_chat, "ainvoke", invoke)
    provider = OpenAICompatibleProvider("key", "https://example.test/v1", "model")
    service = CrossUnitCoordinationService(provider)
    with pytest.raises(asyncio.CancelledError):
        await service.run(state())
    assert len(calls) == (2 if during_repair else 1)
    assert any(item["status"] == "unknown" for item in service.runtime.data["calls"].values())
    if during_repair:
        assert any(item.get("coordination_error_code") == "decision_followups_conflict"
                   for item in service.runtime.data["calls"].values())
