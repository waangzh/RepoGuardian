import asyncio
import json

import pytest
from langchain_core.messages import AIMessage

from app.agents.providers import OpenAICompatibleProvider
from app.models.review import UnitReviewRecord, UnitReviewResponse
from app.services.review_run_budget import ReviewRunBudget, activate_run_budget
from app.services.review_unit_executor import ReviewUnitExecutor
from app.services.model_request_budgeter import ModelRequestProfile
from test_run_scheduler_repairs import large_file, review_unit, review_state


@pytest.mark.parametrize("release", [True, False])
async def test_single_worksets_pass_preflight_then_compete_at_send(release):
    both_ready, sending, rejected = asyncio.Event(), asyncio.Event(), asyncio.Event()
    inspected, first_rejections, requests = set(), [], []
    budget = ReviewRunBudget("race", max_tokens=25000, max_calls=5)

    class Provider(OpenAICompatibleProvider):
        async def review_step(self, pr, files, diff, model, record_input):
            path = files[0].file_path
            inspected.add(path)
            if len(inspected) == 2:
                both_ready.set()
            await asyncio.wait_for(both_ready.wait(), 5)
            if path == "b.py" and not requests:
                await asyncio.wait_for(sending.wait(), 5)
            await self._request_json_content("x" * (20000 if path == "a.py" else 6000),
                "fixture", "review_step", "s", 10)
            return UnitReviewResponse(review_record=UnitReviewRecord(change_summary="check", target_checks=[{
                "target": target, "status": "checked", "reason": "original evidence",
                "evidence_ids": [item["id"] for item in record_input["evidence"]],
            } for target in record_input["targets"]]))

        def unit_review_step_admission(self, *args):
            return {"reserved_tokens": 1000, "estimated_input_tokens": 990}

    provider = Provider("key", "https://example.test/v1", "fixture", request_attempts=1,
                        request_profile=ModelRequestProfile(context_window=100000))
    class Chat:
        async def ainvoke(self, messages):
            requests.append(messages)
            if len(messages[-1].content) > 10000:
                sending.set()
                await asyncio.wait_for(rejected.wait(), 5)
            total = 2 if release else 25000
            return AIMessage(content="{}", usage_metadata={"input_tokens": total - 1,
                "output_tokens": 1, "total_tokens": total})
    provider._build_chat_model = lambda *args: Chat()

    class Executor(ReviewUnitExecutor):
        async def _execute_unit(self, unit, state):
            result = await super()._execute_unit(unit, state)
            if unit.id == "b.py" and result.error == "run_budget_admission_rejected":
                first_rejections.append(result)
                rejected.set()
            return result
    executor = Executor(provider, concurrency=2, timeout_seconds=10)
    state = review_state()
    file = large_file(count=1, width=1)
    state["changed_files"] = [file.model_dump(mode="json"),
        file.model_copy(update={"file_path": "b.py"}).model_dump(mode="json")]
    state["file_index"] = [{"path": path} for path in ["a.py", "b.py"]]
    units = [review_unit().model_copy(update={"id": path, "primary_files": [path], "fingerprint": path})
             for path in ["a.py", "b.py"]]
    with activate_run_budget(budget):
        results = await executor.execute(units, state)
    assert len(first_rejections) == 1
    assert first_rejections[0].diff_batches == []
    refusal = first_rejections[0].budget_rejection
    assert refusal.kind == "budget" and refusal.can_defer
    assert refusal.budget_revision >= 0
    from app.models.review import ReviewUnitResult
    assert ReviewUnitResult.model_validate_json(first_rejections[0].model_dump_json()).budget_rejection == refusal
    if release:
        assert all(item.status == "completed" for item in results), [item.error for item in results]
        assert budget.calls_consumed == len(requests) == 2
    else:
        assert results[1].status == "failed"
        assert budget.calls_consumed == len(requests) == 1


def test_old_unit_result_remains_readable_without_budget_rejection():
    from app.models.review import ReviewUnitResult
    raw = {"review_unit_id": "old", "status": "failed", "error": "transport_failure"}
    restored = ReviewUnitResult.model_validate(json.loads(json.dumps(raw)))
    assert restored.budget_rejection is None


def canonical_request_args():
    from app.models.review import PullRequestInfo
    from app.services.unit_review_summary import build_record_input

    file = large_file(count=1, width=1)
    return (PullRequestInfo.model_validate(review_state()["pr_info"]), [file], "+return value", None,
            build_record_input([file], [], None, "head-a"))


async def test_review_step_preflight_and_send_share_complete_prepared_request():
    from app.review.review_policy import REVIEW_STEP_OUTPUT_TOKENS, REVIEW_STEP_PROTOCOL_VERSION

    provider = OpenAICompatibleProvider("key", "https://example.test/v1", "fixture", request_attempts=1,
        request_profile=ModelRequestProfile(context_window=100000))
    prepared, sent, output_limits = [], [], []
    builder = provider.prepare_review_step_request
    def prepare(*args):
        request = builder(*args)
        prepared.append(request)
        return request
    provider.prepare_review_step_request = prepare
    class Chat:
        async def ainvoke(self, messages):
            sent.append(messages)
            return AIMessage(content='{"issues": [], "next_need": null}')
    def chat(model, output_tokens):
        output_limits.append(output_tokens)
        return Chat()
    provider._build_chat_model = chat
    executor = ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=10)
    args = canonical_request_args()
    admission = executor._diagnosis_estimate(args)
    assert sent == []
    response = await provider.review_step(*args)
    assert prepared[0] == prepared[1]
    assert response.usage.response_metadata["request_admission"] == admission
    assert admission["reserved_tokens"] == admission["estimated_input_tokens"] + REVIEW_STEP_OUTPUT_TOKENS
    assert output_limits == [prepared[0].output_tokens] == [REVIEW_STEP_OUTPUT_TOKENS]
    assert sent[0][0].content == prepared[0].system
    assert sent[0][1].content == prepared[0].prompt
    assert prepared[0].prompt.startswith(f"协议版本：{REVIEW_STEP_PROTOCOL_VERSION}\n")
    assert "next_need" in prepared[0].prompt


async def test_review_step_preflight_rejects_protocol_overhead_before_send():
    from app.agents.providers import LLMProviderError

    provider = OpenAICompatibleProvider("key", "https://example.test/v1", "fixture", request_attempts=1,
        request_profile=ModelRequestProfile(context_window=100000))
    provider._build_unit_review_prompt = lambda *args: "same basic input"
    args = canonical_request_args()
    old = provider.unit_diagnosis_admission(*args)
    new = provider.unit_review_step_admission(*args)
    assert new["estimated_input_tokens"] > old["estimated_input_tokens"]
    provider = OpenAICompatibleProvider("key", "https://example.test/v1", "fixture", request_attempts=1,
        request_profile=ModelRequestProfile(context_window=100000, max_input_tokens=old["estimated_input_tokens"]))
    provider._build_unit_review_prompt = lambda *args: "same basic input"
    provider._build_chat_model = lambda *args: pytest.fail("capacity rejection must precede SDK creation")
    executor = ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=10)
    assert provider.unit_diagnosis_admission(*args)["request_hash"] == old["request_hash"]
    with pytest.raises(LLMProviderError, match="model_input_limit_exceeded"):
        executor._diagnosis_estimate(args)
    from app.models.review import ExecutionBudget
    admission = executor._workset_admission(review_unit(), args[1], review_state(), ExecutionBudget())
    assert not admission["admitted"] and admission["can_split"]
    assert admission["reason"] == "model_input_limit_exceeded"
    with pytest.raises(LLMProviderError, match="model_input_limit_exceeded"):
        await provider.review_step(*args)
