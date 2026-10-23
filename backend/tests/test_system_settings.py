import pytest
from pydantic import ValidationError

from app.models.operations import SystemSettingsUpdate


def test_system_settings_accept_bounded_values_and_safe_model_url():
    value = SystemSettingsUpdate(
        retention_days=30,
        model_base_url="https://api.example.test/v1/",
        model_name="custom-model",
    )
    assert value.model_base_url == "https://api.example.test/v1"


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "https://user:password@example.test/v1",
    "https://example.test/v1?token=secret",
    "not-a-url",
])
def test_system_settings_reject_unsafe_model_urls(url):
    with pytest.raises(ValidationError):
        SystemSettingsUpdate(model_base_url=url)
