"""从当前 OpenAI 兼容 Provider 查询可用模型。"""

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.models.operations import ModelCatalogResponse, ProviderModelInfo


class ModelCatalogError(RuntimeError):
    pass


class _ProviderModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)
    owned_by: str | None = None


class _ProviderModelList(BaseModel):
    model_config = ConfigDict(extra="ignore")

    data: list[_ProviderModel]


async def fetch_model_catalog(
    provider: str,
    api_key: str | None,
    base_url: str,
    default_model: str,
) -> ModelCatalogResponse:
    """调用 OpenAI 兼容的 GET /models，并返回去重、排序后的模型列表。"""
    if not api_key:
        raise ModelCatalogError("模型服务 API Key 未配置")

    url = f"{base_url.rstrip('/')}/models"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            response = await client.get(
                url,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
        response.raise_for_status()
        payload = _ProviderModelList.model_validate(response.json())
    except (httpx.HTTPError, ValueError, ValidationError) as exc:
        raise ModelCatalogError("无法从模型服务获取模型列表") from exc

    unique_models = {
        item.id: ProviderModelInfo(id=item.id, owned_by=item.owned_by)
        for item in payload.data
        if item.id.strip()
    }
    if not unique_models:
        raise ModelCatalogError("模型服务未返回可用模型")

    return ModelCatalogResponse(
        provider=provider,
        default_model=default_model,
        models=sorted(unique_models.values(), key=lambda item: item.id.casefold()),
    )
