import json
from types import SimpleNamespace

import pytest
import httpx
from langchain_core.messages import AIMessage

from app.agents.providers import OpenAICompatibleProvider
from app.evaluation.data import FrozenSample
from app.evaluation.runner import run_sample
from app.services.review_run_budget import current_run_budget


@pytest.mark.parametrize("versions", [None, {"prompt_version": "task-review-prompt-v5", "risk_planner_version": "risk-planner-v2"}])
async def test_experiment_sample_rejects_version_drift_before_graph_or_budget(monkeypatch, tmp_path, versions):
    monkeypatch.setattr("app.evaluation.runner.settings.repoguardian_review_engine", "task_review_v1")
    monkeypatch.setattr("app.evaluation.runner.settings.repoguardian_model", "deepseek-flash")
    sample = FrozenSample(instance_id="local__sample@2222222", repository="local/sample",
        pr_url="https://github.com/local/sample/pull/1", base_sha="1" * 40, head_sha="2" * 40,
        language="Python", changed_lines=1)
    provider = OpenAICompatibleProvider("fixture", "https://api.deepseek.com", "deepseek-flash", request_attempts=1)
    def forbidden(*args, **kwargs):
        pytest.fail("version mismatch must reject before graph construction or budget creation")
    monkeypatch.setattr("app.evaluation.runner.build_review_graph", forbidden)
    monkeypatch.setattr("app.evaluation.runner.ReviewRunBudget.load_or_create", forbidden)
    with pytest.raises(ValueError, match="versions changed before dispatch"):
        await run_sample(sample, provider, tmp_path, git_tool=object(),
                         experiment={"runtime_versions": versions})
    assert not list(tmp_path.iterdir()) and current_run_budget() is None


@pytest.mark.parametrize("usage_known", [True, False])
async def test_evaluation_activates_and_exports_each_attempt(monkeypatch, tmp_path, usage_known):
    monkeypatch.setattr("app.evaluation.runner.settings.repoguardian_model", "deepseek-flash")
    sample = FrozenSample(instance_id="local__sample@2222222", repository="local/sample",
        pr_url="https://github.com/local/sample/pull/1", base_sha="1" * 40, head_sha="2" * 40,
        language="Python", changed_lines=1)
    provider = OpenAICompatibleProvider("fixture", "https://api.deepseek.com", "deepseek-flash",
        disable_thinking=True, request_attempts=1)

    class Chat:
        async def ainvoke(self, messages):
            return AIMessage(content="{}", **({"usage_metadata": {
                "input_tokens": 2, "output_tokens": 1, "total_tokens": 3}} if usage_known else {}))

    provider._build_chat_model = lambda *args: Chat()

    class Graph:
        async def astream(self, state, **kwargs):
            assert current_run_budget() is not None
            await state["_provider"]._request_json_content("return JSON", None, "review_step", "JSON", 32)
            yield {"report": {"task_runtime_report": {"execution_status": "completed", "scope_complete": True},
                              "review_units": [], "review_unit_results": [], "review_issues": []}}

    monkeypatch.setattr("app.evaluation.runner.build_review_graph",
        lambda **kwargs: SimpleNamespace(compile=lambda: Graph()))
    result = await run_sample(sample, provider, tmp_path, git_tool=object())
    assert result["status"] == "completed"
    budget = result["run_budget"]
    attempt, = budget["attempts"].values()
    assert budget["calls_consumed"] == 1
    assert attempt["sent"] and attempt["usage_available"] == usage_known
    if usage_known:
        assert attempt["released_tokens"] == attempt["reserved_tokens"] - 3
        assert budget["settled_actual"] == 3 and budget["unknown_reserved"] == 0
    else:
        assert attempt["actual_tokens"] is None and attempt["released_tokens"] == 0
        assert budget["unknown_reserved"] == attempt["reserved_tokens"]
    assert result["effective_model_profile"]["source"] == "known_model_registry"
    assert result["effective_model_profile"]["profile"]["context_window"] == 1_000_000
    assert current_run_budget() is None
    assert "fixture" not in json.dumps(budget)


async def test_deepseek_thinking_is_serialized_in_http_body(monkeypatch):
    import app.agents.providers as providers

    wire = []
    def transport(request):
        wire.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "offline", "object": "chat.completion", "created": 0,
            "model": "deepseek-flash", "choices": [{"index": 0, "message": {
                "role": "assistant", "content": "{}"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}})

    original = providers.ChatOpenAI
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        monkeypatch.setattr(providers, "ChatOpenAI", lambda **kwargs: original(**kwargs,
            http_async_client=client, http_socket_options=()))
        provider = providers.build_provider("deepseek", "fixture", "https://api.deepseek.com", "deepseek-flash")
        await provider._request_json_content("return JSON", None, "review_step", "JSON", 32)
    assert wire[0]["thinking"] == {"type": "disabled"}
    assert wire[0]["response_format"] == {"type": "json_object"}
