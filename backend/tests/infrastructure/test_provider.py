from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.llm.providers import LLMProviderError, OpenAICompatibleProvider, build_provider
from app.models.review import ChangedFile, PullRequestInfo, PullRequestRef


class FakeChatOpenAI:
    responses: list[AIMessage | Exception] = []
    instances: list["FakeChatOpenAI"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.messages: list[SystemMessage | HumanMessage] = []
        type(self).instances.append(self)

    async def ainvoke(self, messages: list[SystemMessage | HumanMessage]) -> AIMessage:
        self.messages = messages
        response = type(self).responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def fake_chat(monkeypatch: pytest.MonkeyPatch) -> type[FakeChatOpenAI]:
    FakeChatOpenAI.responses = []
    FakeChatOpenAI.instances = []
    monkeypatch.setattr("app.llm.providers.ChatOpenAI", FakeChatOpenAI)
    return FakeChatOpenAI


def _sample_pr() -> PullRequestInfo:
    return PullRequestInfo(
        owner="local", repo="sample", number=1, title="Fix stats",
        html_url="https://github.com/local/sample/pull/1",
        clone_url="https://github.com/local/sample.git",
        base=PullRequestRef(ref="main", sha="1" * 40, repo_clone_url="https://github.com/local/sample.git"),
        head=PullRequestRef(ref="feature", sha="2" * 40, repo_clone_url="https://github.com/local/sample.git"),
    )


@pytest.mark.asyncio
async def test_provider_constructs_chatopenai_with_json_mode_and_model_override(
    fake_chat: type[FakeChatOpenAI],
) -> None:
    fake_chat.responses = [AIMessage(content='{"summary":"修复区间统计。"}')]
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "default-model")

    await provider.summarize_pr_purpose(_sample_pr(), [], "override-model")

    chat = fake_chat.instances[0]
    assert chat.kwargs == {
        "api_key": "key",
        "base_url": "https://example.com/v1",
        "model": "override-model",
        "temperature": 0.1,
        "max_tokens": 700,
        "max_retries": 0,
        "timeout": 60.0,
        "model_kwargs": {"response_format": {"type": "json_object"}},
    }
    assert isinstance(chat.messages[0], SystemMessage)
    assert isinstance(chat.messages[1], HumanMessage)


@pytest.mark.asyncio
async def test_provider_summarizes_english_pr_purpose_in_chinese(
    fake_chat: type[FakeChatOpenAI],
) -> None:
    fake_chat.responses = [AIMessage(content='{"summary":"修复区间统计，使点估计始终位于区间内。"}')]
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")

    summary = await provider.summarize_pr_purpose(
        _sample_pr(),
        [ChangedFile(file_path="stats.py", change_type="modified", additions=3, deletions=1)],
        None,
    )

    assert summary.value == "修复区间统计，使点估计始终位于区间内。"


@pytest.mark.asyncio
async def test_provider_rejects_pr_purpose_without_chinese(
    fake_chat: type[FakeChatOpenAI],
) -> None:
    fake_chat.responses = [AIMessage(content='{"summary":"Fix interval statistics."}')]
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")

    with pytest.raises(LLMProviderError):
        await provider.summarize_pr_purpose(_sample_pr(), [], None)


@pytest.mark.asyncio
async def test_deepseek_passes_thinking_control_via_extra_body(
    fake_chat: type[FakeChatOpenAI],
) -> None:
    fake_chat.responses = [AIMessage(content='{"summary":"修复区间统计。"}')]
    provider = build_provider("deepseek", "key", "https://api.deepseek.com", "deepseek-model")

    await provider.summarize_pr_purpose(_sample_pr(), [], None)

    assert fake_chat.instances[0].kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


@pytest.mark.asyncio
async def test_openai_compatible_deepseek_endpoint_disables_thinking(
    fake_chat: type[FakeChatOpenAI],
) -> None:
    fake_chat.responses = [AIMessage(content='{"summary":"修复区间统计。"}')]
    provider = build_provider("openai-compatible", "key", "https://api.deepseek.com", "deepseek-model")

    await provider.summarize_pr_purpose(_sample_pr(), [], None)

    assert fake_chat.instances[0].kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


@pytest.mark.asyncio
async def test_provider_rejects_invalid_json(fake_chat: type[FakeChatOpenAI]) -> None:
    fake_chat.responses = [AIMessage(content="not json")]
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")

    with pytest.raises(LLMProviderError):
        await provider.summarize_pr_purpose(_sample_pr(), [], None)


@pytest.mark.asyncio
async def test_provider_wraps_chatopenai_failures(fake_chat: type[FakeChatOpenAI]) -> None:
    fake_chat.responses = [RuntimeError("network unavailable")]
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")

    with pytest.raises(LLMProviderError, match="RuntimeError: network unavailable") as raised:
        await provider.summarize_pr_purpose(_sample_pr(), [], None)
    assert raised.value.usage
    assert raised.value.usage.usage_available is False
    assert raised.value.usage.response_metadata["request_attempts"] == 1


@pytest.mark.asyncio
async def test_provider_retries_retryable_connection_failure(
    fake_chat: type[FakeChatOpenAI],
) -> None:
    class APIConnectionError(RuntimeError):
        pass

    fake_chat.responses = [
        APIConnectionError("temporary outage"),
        AIMessage(content='{"summary":"修复区间统计。"}'),
    ]
    provider = OpenAICompatibleProvider(
        "key", "https://example.com/v1", "model", retry_backoff_seconds=0
    )

    result = await provider.summarize_pr_purpose(_sample_pr(), [], None)

    assert result.value == "修复区间统计。"
    assert len(fake_chat.instances[0].messages) == 2
    assert fake_chat.responses == []


@pytest.mark.asyncio
async def test_provider_redacts_credentials_from_failure_detail(
    fake_chat: type[FakeChatOpenAI],
) -> None:
    fake_chat.responses = [
        RuntimeError("Authorization: Bearer secret-token api_key=secret-token")
    ]
    provider = OpenAICompatibleProvider(
        "secret-token", "https://example.com/v1", "model"
    )

    with pytest.raises(LLMProviderError) as raised:
        await provider.summarize_pr_purpose(_sample_pr(), [], None)

    assert "secret-token" not in str(raised.value)
    assert "secret-token" not in str(raised.value.usage.response_metadata)


def test_provider_rejects_non_string_ai_message_content() -> None:
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")

    with pytest.raises(LLMProviderError, match="missing string content"):
        provider._extract_message_content(AIMessage(content=[{"type": "text", "text": "{}"}]))


def test_build_provider_supports_expected_names() -> None:
    assert isinstance(build_provider("deepseek", "key", "https://api.deepseek.com", "model"),
                      OpenAICompatibleProvider)
    assert isinstance(build_provider("openai-compatible", "key", "https://example.com/v1", "model"),
                      OpenAICompatibleProvider)


def test_build_provider_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="REPOGUARDIAN_PROVIDER"):
        build_provider("unknown", "key", "https://example.com/v1", "model")


@pytest.mark.asyncio
async def test_task_review_step_requires_matching_operation(fake_chat: type[FakeChatOpenAI]) -> None:
    from app.llm.model_request_budgeter import PreparedModelRequest

    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")
    request = PreparedModelRequest(model="model", operation="diagnosis", system="s",
                                   prompt="p", output_tokens=10)

    with pytest.raises(LLMProviderError, match="task_review_v1_request_required"):
        await provider.task_review_step(request)
