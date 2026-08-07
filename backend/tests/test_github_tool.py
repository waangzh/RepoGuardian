import pytest

from app.tools.github_tool import GitHubTool, parse_pr_url


def test_parse_pr_url() -> None:
    assert parse_pr_url("https://github.com/openai/codex/pull/12") == ("openai", "codex", 12)


def test_parse_pr_url_rejects_invalid_url() -> None:
    with pytest.raises(ValueError):
        parse_pr_url("https://example.com/openai/codex/pull/12")


@pytest.mark.asyncio
async def test_fetch_pr_preserves_body(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "title": "Explain the change",
        "body": "This PR fixes interval handling.",
        "html_url": "https://github.com/openai/codex/pull/12",
        "base": {
            "ref": "main",
            "sha": "a" * 40,
            "repo": {"clone_url": "https://github.com/openai/codex.git"},
        },
        "head": {
            "ref": "fix",
            "sha": "b" * 40,
            "repo": {"clone_url": "https://github.com/openai/codex.git"},
        },
    }

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self) -> dict:
            return payload

    class FakeAsyncClient:
        def __init__(self, *, timeout: int) -> None:
            assert timeout == 30

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, url: str, *, headers: dict[str, str]) -> FakeResponse:
            assert url.endswith("/repos/openai/codex/pulls/12")
            assert headers["Accept"] == "application/vnd.github+json"
            return FakeResponse()

    monkeypatch.setattr("app.tools.github_tool.httpx.AsyncClient", FakeAsyncClient)

    pr = await GitHubTool().fetch_pr("https://github.com/openai/codex/pull/12")

    assert pr.body == "This PR fixes interval handling."
