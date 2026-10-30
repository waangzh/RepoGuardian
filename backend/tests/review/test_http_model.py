import json

import httpx
from openai import AsyncOpenAI

from app.llm.providers import OpenAICompatibleProvider
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
