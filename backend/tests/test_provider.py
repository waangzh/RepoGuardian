import pytest

from app.agents.providers import LLMProviderError, MockProvider, OpenAICompatibleProvider, build_provider


def test_openai_provider_rejects_invalid_json() -> None:
    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")

    with pytest.raises(LLMProviderError):
        provider._parse_issues("not json")


def test_build_provider_supports_deepseek_alias() -> None:
    provider = build_provider("deepseek", "key", "https://api.deepseek.com", "deepseek-v4-pro")

    assert isinstance(provider, OpenAICompatibleProvider)


def test_build_provider_normalizes_provider_name() -> None:
    provider = build_provider(" OpenAI-Compatible ", "key", "https://example.com", "model")

    assert isinstance(provider, OpenAICompatibleProvider)


def test_build_provider_supports_mock() -> None:
    provider = build_provider("mock", None, "https://example.com", "model")

    assert isinstance(provider, MockProvider)


def test_build_provider_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="REPOGUARDIAN_PROVIDER"):
        build_provider("unknown", None, "https://example.com", "model")