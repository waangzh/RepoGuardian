from datetime import timezone

import pytest
from fastapi import HTTPException

from app.api.validation_backends import (
    get_validation_backend,
    get_validation_backend_profiles,
    list_validation_backends,
)


@pytest.mark.asyncio
async def test_backend_discovery_is_read_only_and_exposes_registered_profiles() -> None:
    backends = await list_validation_backends()
    by_name = {item.name: item for item in backends}

    assert set(by_name) == {"none", "user_runner", "project_ci", "gvisor"}
    assert by_name["user_runner"].supported_profiles == ["unit", "lint"]
    assert by_name["project_ci"].executes_untrusted_code is True
    assert by_name["none"].executes_untrusted_code is False
    assert by_name["user_runner"].last_health_check_at.tzinfo == timezone.utc
    assert not hasattr(by_name["user_runner"], "command")


@pytest.mark.asyncio
async def test_backend_detail_and_profiles_share_server_policy() -> None:
    detail = await get_validation_backend("user_runner")
    profiles = await get_validation_backend_profiles("user_runner")

    assert profiles.backend == detail.name
    assert profiles.profiles == detail.supported_profiles
    with pytest.raises(HTTPException) as exc_info:
        await get_validation_backend("model-invented")
    assert exc_info.value.status_code == 404
