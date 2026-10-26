from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.llm.providers import LLMProviderError, OpenAICompatibleProvider


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


@pytest.mark.asyncio
async def test_task_review_step_requires_matching_operation(fake_chat: type[FakeChatOpenAI]) -> None:
    from app.llm.model_request_budgeter import PreparedModelRequest

    provider = OpenAICompatibleProvider("key", "https://example.com/v1", "model")
    request = PreparedModelRequest(model="model", operation="diagnosis", system="s",
                                   prompt="p", output_tokens=10)

    with pytest.raises(LLMProviderError, match="task_review_v1_request_required"):
        await provider.task_review_step(request)
