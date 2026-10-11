import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from app.agents.providers import OpenAICompatibleProvider
from app.evaluation.data import FrozenSample
from app.evaluation.runner import run_sample
from app.services.review_run_budget import current_run_budget


@pytest.mark.parametrize("usage_known", [True, False])
async def test_evaluation_activates_and_exports_each_attempt(monkeypatch, tmp_path, usage_known):
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
            yield {"report": {"review_units": [], "review_unit_results": [], "review_issues": []}}

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
