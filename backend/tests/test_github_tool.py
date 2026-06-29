import pytest

from app.tools.github_tool import parse_pr_url


def test_parse_pr_url() -> None:
    assert parse_pr_url("https://github.com/openai/codex/pull/12") == ("openai", "codex", 12)


def test_parse_pr_url_rejects_invalid_url() -> None:
    with pytest.raises(ValueError):
        parse_pr_url("https://example.com/openai/codex/pull/12")

