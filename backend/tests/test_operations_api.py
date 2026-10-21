from types import SimpleNamespace

import httpx
import pytest

from app.api import system as system_api
from app.main import app


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/api/validation/backends",
    "/api/validation/backends/user_runner",
    "/api/validation/backends/user_runner/profiles",
])
async def test_retired_backend_discovery_endpoints_return_not_found(path: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(path)

    assert response.status_code == 404
    assert not any(
        route.startswith("/api/validation/backends") for route in app.openapi()["paths"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("enabled", "mode"), [(False, "copy_only"), (True, "publish")])
async def test_publication_capability_reports_mode_without_secrets(
    monkeypatch: pytest.MonkeyPatch, enabled: bool, mode: str
) -> None:
    """发布页进入前探测能力；只返回是否启用，不泄露凭据或仓库范围。"""
    monkeypatch.setattr(
        system_api.settings, "repoguardian_github_publish_enabled", enabled
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/system/publication-capability")

    assert response.status_code == 200
    assert response.json() == {"enabled": enabled, "mode": mode}


@pytest.mark.asyncio
async def test_system_diagnostics_remains_available_without_backend_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        system_api, "get_review_service", lambda: SimpleNamespace(_worker_task=None)
    )
    monkeypatch.setattr(system_api, "schema_is_current", lambda: True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/system/diagnostics")

    assert response.status_code == 200
    payload = response.json()
    assert payload["database_schema_current"] is True
    assert payload["worker_status"] == "idle"
    assert "validation_backends" not in payload
