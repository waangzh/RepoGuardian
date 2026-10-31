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


def test_cumulative_token_and_total_call_limits_are_optional(monkeypatch):
    monkeypatch.setattr(settings, "repoguardian_review_max_model_calls", None)
    monkeypatch.setattr(settings, "repoguardian_review_max_input_tokens", None)
    monkeypatch.setattr(settings, "repoguardian_review_max_output_tokens", None)
    monkeypatch.setattr(settings, "repoguardian_model_context_tokens", 1_000_000)
    provider = SimpleNamespace(_provider_name="test", _base_url="http://localhost",
                               _default_model="fake")
    frozen = freeze_configuration(ReviewRequest(pr_url="https://github.com/o/r/pull/1"), provider)
    limits = frozen.options.limits
    assert limits.total_model_calls is None
    assert limits.total_input_tokens is None
    assert limits.total_output_tokens is None
    assert limits.context_tokens == 1_000_000
