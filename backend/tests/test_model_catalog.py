from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from app.api import system
from app.models.operations import ModelCatalogResponse
from app.services.model_catalog import ModelCatalogError, fetch_model_catalog


class FakeAsyncClient:
    response: httpx.Response
    request: httpx.Request | None = None
    options: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        type(self).options = kwargs

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def get(self, url: str, headers: dict[str, str]) -> httpx.Response:
        type(self).request = httpx.Request("GET", url, headers=headers)
        self.response.request = type(self).request
        return self.response


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> type[FakeAsyncClient]:
    FakeAsyncClient.request = None
    FakeAsyncClient.response = httpx.Response(200, json={"data": []})
    monkeypatch.setattr("app.services.model_catalog.httpx.AsyncClient", FakeAsyncClient)
    return FakeAsyncClient


@pytest.mark.asyncio
async def test_fetch_model_catalog_uses_openai_compatible_models_endpoint(
    fake_client: type[FakeAsyncClient],
) -> None:
    fake_client.response = httpx.Response(200, json={
        "object": "list",
        "data": [
            {"id": "gpt-4.1-mini", "object": "model", "owned_by": "openai"},
            {"id": "gpt-4.1", "object": "model", "owned_by": "openai"},
            {"id": "gpt-4.1", "object": "model", "owned_by": "openai"},
        ],
    })

    catalog = await fetch_model_catalog(
        "openai", "secret-key", "https://api.openai.com/v1/", "gpt-4.1-mini"
    )

    assert [model.id for model in catalog.models] == ["gpt-4.1", "gpt-4.1-mini"]
    assert catalog.default_model == "gpt-4.1-mini"
    assert fake_client.request is not None
    assert str(fake_client.request.url) == "https://api.openai.com/v1/models"
    assert fake_client.request.headers["authorization"] == "Bearer secret-key"
    assert FakeAsyncClient.options == {"timeout": 15, "follow_redirects": True}


@pytest.mark.asyncio
async def test_fetch_model_catalog_supports_deepseek_base_url(
    fake_client: type[FakeAsyncClient],
) -> None:
    fake_client.response = httpx.Response(200, json={
        "data": [{"id": "deepseek-chat", "owned_by": "deepseek"}],
    })

    catalog = await fetch_model_catalog(
        "deepseek", "secret-key", "https://api.deepseek.com", "deepseek-chat"
    )

    assert catalog.models[0].id == "deepseek-chat"
    assert fake_client.request is not None
    assert str(fake_client.request.url) == "https://api.deepseek.com/models"


@pytest.mark.asyncio
async def test_fetch_model_catalog_rejects_missing_key() -> None:
    with pytest.raises(ModelCatalogError, match="API Key 未配置"):
        await fetch_model_catalog("openai", None, "https://api.openai.com/v1", "gpt-4.1-mini")


@pytest.mark.asyncio
async def test_fetch_model_catalog_wraps_invalid_response(
    fake_client: type[FakeAsyncClient],
) -> None:
    fake_client.response = httpx.Response(200, json={"unexpected": []})

    with pytest.raises(ModelCatalogError, match="无法从模型服务获取模型列表"):
        await fetch_model_catalog("openai", "key", "https://example.com/v1", "model")


@pytest.mark.asyncio
async def test_models_api_returns_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = ModelCatalogResponse(
        provider="openai",
        default_model="gpt-4.1-mini",
        models=[{"id": "gpt-4.1-mini", "owned_by": "openai"}],
    )

    async def fake_fetch_model_catalog(**kwargs: object) -> ModelCatalogResponse:
        assert kwargs["api_key"] == system.settings.openai_api_key
        return expected

    monkeypatch.setattr(system, "fetch_model_catalog", fake_fetch_model_catalog)

    assert await system.get_available_models() == expected


@pytest.mark.asyncio
async def test_models_api_translates_provider_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch_model_catalog(**kwargs: object) -> ModelCatalogResponse:
        del kwargs
        raise ModelCatalogError("模型服务不可用")

    monkeypatch.setattr(system, "fetch_model_catalog", fake_fetch_model_catalog)

    with pytest.raises(HTTPException) as exc_info:
        await system.get_available_models()

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "模型服务不可用"
