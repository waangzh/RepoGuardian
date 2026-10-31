import json
from datetime import timedelta

import httpx
import pytest
from openai import AsyncOpenAI
from sqlalchemy import select

from app.llm.providers import OpenAICompatibleProvider
from app.models.orm import ReviewRequestOrm, ReviewTaskOrm
from app.review.agent import AgentRunner
from app.review.domain import utcnow
from app.review.orchestrator import ReviewOrchestrator
from app.review.planner import tokens
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


@pytest.mark.parametrize("mode", ["recover", "repeat", "exhausted", "budget"])
async def test_plain_text_verifier_requires_bounded_structured_correction(tmp_path, mode, caplog):
    store, run, lease, provider, sessions = setup_source_run(tmp_path)
    run.configuration.options.limits.model_calls = 1 if mode == "exhausted" else 12
    if mode == "budget":
        run.configuration.options.limits.total_model_calls = 3
    with sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
    requests, verifier_requests = [], []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        names = {tool["function"]["name"] for tool in payload["tools"]}
        packet = json.loads(payload["messages"][1]["content"])
        calls, content = [], None
        if "OverviewOutput" in names:
            results = [("OverviewOutput", {
                "change_overview": [{"text": "调整比例计算。", "source_refs": ["S1"]}],
            })]
        elif "UnitCompletion" in names:
            handle = next(source["source_ref"] for material in packet["materials"]
                          for source in material["sources"] if source["source"]["side"] == "head")
            results = [
                ("submit_candidate", {"proposal": {"claim": "零输入导致除零", "trigger": "x=0",
                    "impact": "请求失败", "source_refs": [handle]}}),
                ("UnitCompletion", {}),
            ]
        else:
            verifier_requests.append(payload)
            result = {"verdict": "confirmed", "attribution": "introduced", "severity": "high",
                "reason": "BASE 使用非零回退，HEAD 在零输入时除零。",
                "source_refs": [source["source_ref"] for material in packet["materials"]
                                for source in material["sources"]],
                "author_comment": "零输入会触发除零，请恢复回退或显式处理。"}
            if len(verifier_requests) == 1 or mode == "repeat":
                # Valid-looking prose/JSON is still not an admitted tool result.
                content = "PRIVATE_MODEL_FREE_TEXT\n" + json.dumps(result)
                results = []
            else:
                assert names == {"VerificationOutput"}
                results = [("VerificationOutput", result)]
        for index, (name, result) in enumerate(results):
            calls.append({"id": f"call-{len(requests)}-{index}", "type": "function", "function": {
                "name": name, "arguments": json.dumps(result)}})
        return httpx.Response(200, json={"id": "chat", "object": "chat.completion", "created": 1,
            "model": "fixture", "choices": [{"index": 0, "finish_reason": "tool_calls" if calls else "stop",
                "message": {"role": "assistant", "content": content, "tool_calls": calls}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    model = OpenAICompatibleProvider("fixture", "https://example.test/v1", "fixture").agent_model()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        sdk = AsyncOpenAI(api_key="fixture", base_url="https://example.test/v1", http_client=client,
                          max_retries=0)
        model.root_async_client, model.async_client = sdk, sdk.chat.completions
        report = await ReviewOrchestrator(store, AgentRunner(model)).run(run.id, provider, lease)
    assert len(verifier_requests) == (1 if mode in {"exhausted", "budget"} else 2)
    assert len(report.candidates) == 1
    check = next(item for item in report.units if item.kind == "verification")
    if mode == "recover":
        assert report.status == "completed"
        assert check.status == "completed"
        assert len(report.findings) == 1
    elif mode == "budget":
        assert report.status == "partial"
        assert check.status == "interrupted"
        assert check.error_code == "model_call_limit"
        assert report.candidates[0].verification is None
        assert report.findings == []
    else:
        assert report.status == "partial"
        assert check.status == "failed"
        assert check.error_code == "agent_structured_output_missing"
        assert report.candidates[0].verification is None
        assert report.findings == []
    assert "PRIVATE_MODEL_FREE_TEXT" not in caplog.text
    assert report.usage["unknown_calls"] == 0
    assert report.usage["model_calls"] == len(requests)
    with sessions() as session:
        tickets = list(session.scalars(select(ReviewRequestOrm)))
        assert sum(ticket.kind == "verification" for ticket in tickets) == len(verifier_requests)
        assert all(ticket.state == "settled" for ticket in tickets)


@pytest.mark.parametrize("boundary", ["long", "input_tokens", "output_tokens", "time"])
async def test_tokens_and_time_guide_completion_before_call_fuse(tmp_path, boundary, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "repoguardian_model_request_timeout_seconds", 60)
    store, run, lease, provider, sessions = setup_source_run(tmp_path)
    if boundary == "input_tokens":
        run.configuration.options.limits.total_input_tokens = 12000
        run.pr.body = "预算调查上下文。" * 100
    elif boundary == "output_tokens":
        run.configuration.options.limits.total_output_tokens = 8192
    elif boundary == "time":
        run.review_cutoff = utcnow() + timedelta(seconds=30)
    with sessions.begin() as session:
        session.get(ReviewTaskOrm, run.id).task_snapshot = run.model_dump(mode="json")
    requests, review_requests = [], []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        names = {tool["function"]["name"] for tool in payload["tools"]}
        if "OverviewOutput" in names:
            name, result = "OverviewOutput", {
                "change_overview": [{"text": "调整比例计算。", "source_refs": ["S1"]}],
            }
        else:
            review_requests.append(payload)
            assert json.loads(payload["messages"][1]["content"])["depth"] == "standard"
            if len(review_requests) == 1:
                with sessions() as session:
                    reserved = session.scalar(select(ReviewRequestOrm).where(
                        ReviewRequestOrm.run_id == run.id, ReviewRequestOrm.kind == "review",
                        ReviewRequestOrm.state == "reserved"))
                    assert tokens({"messages": payload["messages"], "tools": payload["tools"]}) \
                        <= reserved.reserved_input_tokens
            if "read_file" in names and len(review_requests) <= 13:
                name, result = "read_file", {"path": "a.py", "revision": "head", "start": 1, "end": 2}
            else:
                name, result = "UnitCompletion", {}
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
    assert report.status == "completed", report.reasons
    assert report.overview.status == "ready"
    assert report.units[0].error_code is None
    if boundary == "long":
        assert len(review_requests) == 14
        assert report.units[0].tool_calls == 13
    else:
        assert len(review_requests) == 1
        assert {tool["function"]["name"] for tool in review_requests[0]["tools"]} == {
            "submit_candidate", "UnitCompletion"}
        assert boundary in review_requests[0]["messages"][0]["content"]
    assert report.usage["model_calls"] == len(requests) == len(review_requests) + 1
