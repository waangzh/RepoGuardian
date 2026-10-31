from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.review.configuration import capabilities, freeze_configuration
from app.review.contracts import ReviewRequest
from app.review.domain import Limits


@pytest.mark.parametrize("depth", ["quick", "standard", "deep"])
def test_depth_preserves_strategy_without_small_call_quota(monkeypatch, depth):
    monkeypatch.setattr(settings, "repoguardian_agent_max_calls", 48)
    provider = SimpleNamespace(_provider_name="test", _base_url="http://localhost", _default_model="fake")
    frozen = freeze_configuration(ReviewRequest(pr_url="https://github.com/o/r/pull/1", depth=depth), provider)
    assert frozen.options.depth == depth
    assert frozen.options.limits.model_calls == 48
    assert Limits().model_calls == 48
    presets = capabilities()
    assert all(item["limits"] == presets["limits"]["defaults"] for item in presets["depth_presets"])
    assert frozen.options.limits.model_dump() == presets["limits"]["defaults"]


def test_explicit_call_fuse_is_honored_within_server_policy(monkeypatch):
    monkeypatch.setattr(settings, "repoguardian_agent_max_calls", 48)
    provider = SimpleNamespace(_provider_name="test", _base_url="http://localhost", _default_model="fake")
    request = ReviewRequest(pr_url="https://github.com/o/r/pull/1", depth="quick", limits={"model_calls": 9})
    assert freeze_configuration(request, provider).options.limits.model_calls == 9
    request.limits["model_calls"] = 49
    with pytest.raises(ValueError, match="review_limit_exceeds_server_policy"):
        freeze_configuration(request, provider)
