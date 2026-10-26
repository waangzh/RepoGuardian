"""Native tool-calling model factory; LangChain owns the agent loop."""

from abc import ABC, abstractmethod

from langchain_openai import ChatOpenAI


class LLMProviderError(RuntimeError):
    pass


class LLMProvider(ABC):
    @abstractmethod
    def agent_model(self, model=None, *, output_tokens=4096):
        raise NotImplementedError


class OpenAICompatibleProvider(LLMProvider):
    def __init__(self, api_key, base_url, default_model, disable_thinking=False,
                 provider_name="openai-compatible", request_timeout_seconds=60):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model
        self._disable_thinking = disable_thinking
        self._provider_name = provider_name
        self._request_timeout_seconds = request_timeout_seconds

    def agent_model(self, model=None, *, output_tokens=4096):
        if not self._api_key:
            raise LLMProviderError("agent_credentials_unavailable")
        options = dict(api_key=self._api_key, base_url=self._base_url,
            model=model or self._default_model, temperature=0.1, max_tokens=output_tokens,
            max_retries=0, timeout=self._request_timeout_seconds)
        if self._disable_thinking:
            options["extra_body"] = {"thinking": {"type": "disabled"}}
        return ChatOpenAI(**options)


def build_provider(provider_name, api_key, base_url, default_model):
    from app.core.config import settings
    normalized = provider_name.strip().lower()
    if normalized not in {"openai", "deepseek", "openai-compatible"}:
        raise ValueError("unsupported_provider")
    return OpenAICompatibleProvider(api_key, base_url, default_model,
        disable_thinking=normalized == "deepseek", provider_name=normalized,
        request_timeout_seconds=settings.repoguardian_model_request_timeout_seconds)
