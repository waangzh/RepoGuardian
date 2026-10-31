import json

import httpx
import pytest
from openai import AsyncOpenAI
from sqlalchemy import select

from app.llm.providers import OpenAICompatibleProvider
from app.models.orm import ReviewRequestOrm, ReviewTaskOrm
from app.review.agent import AgentRunner
from app.review.orchestrator import ReviewOrchestrator
from tests.review.vnext_helpers import setup_source_run


async def test_real_provider_adapter_uses_tools_and_all_calls_have_tickets(tmp_path):
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    requests = []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert "response_format" not in payload
        assert "runtime" not in json.dumps(payload["tools"])
        system = payload["messages"][0]["content"]
        packet = json.loads(payload["messages"][1]["content"])
        if system.startswith("Summarize"):
            name = "OverviewOutput"
            result = {"change_overview": [{"text": "调整比例计算。", "source_refs": ["S1"]}],
                      "finding_order": []}
        else:
            name, result = "UnitCompletion", {}
            assert packet["materials"]
        return httpx.Response(200, json={"id": "chat", "object": "chat.completion", "created": 1,
            "model": "fixture", "choices": [{"index": 0, "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "final", "type": "function", "function": {
                        "name": name, "arguments": json.dumps(result)}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    model = OpenAICompatibleProvider("fixture", "https://example.test/v1", "fixture").agent_model()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sdk = AsyncOpenAI(api_key="fixture", base_url="https://example.test/v1", http_client=client,
                          max_retries=0)
        model.root_async_client, model.async_client = sdk, sdk.chat.completions
        report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
    assert report.status == "completed"
    assert report.overview.status == "ready"
    assert len(requests) == report.usage["model_calls"] == 2
    assert report.usage["input_tokens"] == 20


@pytest.mark.parametrize("call_limit", [1, 3])
@pytest.mark.parametrize("unfinished", [False, True])
async def test_last_allowed_request_finishes_instead_of_starting_more_tools(
    tmp_path, call_limit, unfinished,
):
    store, run, lease, provider, sessions = setup_source_run(tmp_path)
    run.configuration.options.limits.model_calls = call_limit
    with sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
    requests = []
    finalizations = []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        names = {tool["function"]["name"] for tool in payload["tools"]}
        if "OverviewOutput" in names:
            name, result = "OverviewOutput", {
                "change_overview": [{"text": "调整比例计算。", "source_refs": ["S1"]}],
            }
        elif names == {"UnitCompletion", "submit_candidate"}:
            finalizations.append(payload)
            name, result = "UnitCompletion", {
                "unresolved_reason": "尚未查明外部调用方的零输入约束。" if unfinished else None,
            }
        else:
            name, result = "read_file", {"path": "a.py", "revision": "head", "start": 1, "end": 2}
        return httpx.Response(200, json={"id": "chat", "object": "chat.completion", "created": 1,
            "model": "fixture", "choices": [{"index": 0, "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": None, "tool_calls": [
                    {"id": f"call-{len(requests)}", "type": "function", "function": {
                        "name": name, "arguments": json.dumps(result)}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    model = OpenAICompatibleProvider("fixture", "https://example.test/v1", "fixture").agent_model()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sdk = AsyncOpenAI(api_key="fixture", base_url="https://example.test/v1", http_client=client,
                          max_retries=0)
        model.root_async_client, model.async_client = sdk, sdk.chat.completions
        report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
    assert len(finalizations) == 1
    assert "Remaining model requests including this one: 1" in finalizations[0]["messages"][0]["content"]
    assert report.status == ("partial" if unfinished else "completed")
    assert report.units[0].status == "completed"
    assert report.units[0].error_code is None
    assert report.units[0].tool_calls == call_limit - 1
    assert report.overview.status == "ready"
    assert len(requests) == report.usage["model_calls"] == call_limit + 1
    with sessions() as session:
        tickets = list(session.scalars(select(ReviewRequestOrm)))
        assert sum(ticket.kind == "review" for ticket in tickets) == call_limit
        assert all(ticket.state == "settled" for ticket in tickets)


@pytest.mark.parametrize("verdict", ["confirmed", "insufficient", "invalid"])
@pytest.mark.parametrize("call_limit", [1, 3])
async def test_verifier_final_request_preserves_admission_and_hard_limit(tmp_path, verdict, call_limit):
    store, run, lease, provider, sessions = setup_source_run(tmp_path)
    run.configuration.options.limits.model_calls = call_limit
    with sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
    requests = []
    verifier_final = []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        names = {tool["function"]["name"] for tool in payload["tools"]}
        packet = json.loads(payload["messages"][1]["content"])
        extra_calls = []
        if "OverviewOutput" in names:
            name, result = "OverviewOutput", {
                "change_overview": [{"text": "调整比例计算。", "source_refs": ["S1"]}],
            }
        elif "UnitCompletion" in names:
            if any(message["role"] == "tool" for message in payload["messages"]):
                name, result = "UnitCompletion", {}
            else:
                handle = next(source["source_ref"] for material in packet["materials"]
                              for source in material["sources"] if source["source"]["side"] == "head")
                name, result = "submit_candidate", {"proposal": {
                    "claim": "零输入导致除零", "trigger": "x=0", "impact": "请求失败",
                    "source_refs": [handle],
                }}
                if call_limit == 1:
                    assert names == {"submit_candidate", "UnitCompletion"}
                    extra_calls.append({"id": "review-complete", "type": "function", "function": {
                        "name": "UnitCompletion", "arguments": "{}"}})
        elif names == {"VerificationOutput"}:
            verifier_final.append(payload)
            handles = [source["source_ref"] for material in packet["materials"]
                       for source in material["sources"]]
            name = "VerificationOutput"
            result = {} if verdict == "invalid" else {
                "verdict": verdict, "attribution": "introduced" if verdict == "confirmed" else "uncertain",
                "reason": "BASE 使用非零回退，HEAD 在零输入时除零。" if verdict == "confirmed"
                          else "尚未查明调用方是否允许零输入。",
                "source_refs": handles,
                "severity": "high" if verdict == "confirmed" else None,
                "author_comment": "零输入导致除零，请恢复回退或显式处理。" if verdict == "confirmed" else None,
            }
        else:
            name, result = "read_file", {"path": "a.py", "revision": "head", "start": 1, "end": 2}
        return httpx.Response(200, json={"id": "chat", "object": "chat.completion", "created": 1,
            "model": "fixture", "choices": [{"index": 0, "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": None, "tool_calls": [
                    {"id": f"call-{len(requests)}", "type": "function", "function": {
                        "name": name, "arguments": json.dumps(result)}}, *extra_calls]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    model = OpenAICompatibleProvider("fixture", "https://example.test/v1", "fixture").agent_model()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sdk = AsyncOpenAI(api_key="fixture", base_url="https://example.test/v1", http_client=client,
                          max_retries=0)
        model.root_async_client, model.async_client = sdk, sdk.chat.completions
        report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
    assert len(verifier_final) == 1
    assert len(report.candidates) == 1
    candidate = report.candidates[0]
    assert len(report.findings) == (1 if verdict == "confirmed" else 0)
    check = next(item for item in report.units if item.kind == "verification")
    if verdict == "invalid":
        assert check.status == "interrupted"
        assert check.error_code == "agent_model_limit"
        assert candidate.verification is None
        assert report.status == "partial"
    else:
        assert check.status == "completed"
        assert candidate.verification.verdict == verdict
    assert next(item for item in report.units if item.kind == "review").status == "completed"
    assert len(requests) == report.usage["model_calls"] == (3 if call_limit == 1 else 6)
    with sessions() as session:
        tickets = list(session.scalars(select(ReviewRequestOrm)))
        assert sum(ticket.kind == "verification" for ticket in tickets) == call_limit
