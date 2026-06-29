import pytest

from app.agents.providers import LLMProviderError, OpenAICompatibleProvider


def test_openai_provider_rejects_invalid_json() -> None:
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")

    with pytest.raises(LLMProviderError):
        provider._parse_issues("not json")

