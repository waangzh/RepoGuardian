import pytest
import asyncio
import httpx

from app.tools.github.github_tool import GitHubTool, GitHubToolError, parse_pr_url


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
            "repo": {"id": 123, "clone_url": "https://github.com/openai/codex.git"},
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

    monkeypatch.setattr("app.tools.github.github_tool.httpx.AsyncClient", FakeAsyncClient)

    pr = await GitHubTool().fetch_pr("https://github.com/openai/codex/pull/12")

    assert pr.body == "This PR fixes interval handling."
    assert pr.repository_id == 123


def mocked_github(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr("app.tools.github.github_tool.httpx.AsyncClient", lambda **kwargs:
        original(transport=httpx.MockTransport(handler), **kwargs))
    return GitHubTool("credential-must-not-leak")


async def test_connection_failure_retries_the_read_and_recovers(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("proxy TLS interrupted; token=secret", request=request)
        return httpx.Response(200, text="diff --git a/a.py b/a.py")
    tool = mocked_github(monkeypatch, handler)
    assert await tool.fetch_diff("https://github.com/o/r/pull/1") == "diff --git a/a.py b/a.py"
    assert len(calls) == 2


@pytest.mark.parametrize("error,code,status", [(httpx.ConnectError, "github_connection_failed", 503),
    (httpx.ProxyError, "github_connection_failed", 503), (httpx.ReadTimeout, "github_timeout", 504)])
async def test_exhausted_read_has_bounded_attempts_and_safe_details(monkeypatch, error, code, status):
    calls = []
    def handler(request):
        calls.append(request)
        raise error("token=secret /private/path", request=request)
    tool = mocked_github(monkeypatch, handler)
    with pytest.raises(GitHubToolError) as caught:
        await tool.fetch_pr("https://github.com/o/r/pull/1")
    assert len(calls) == 3
    assert caught.value.code == code and caught.value.api_status == status
    assert caught.value.retryable and "secret" not in str(caught.value.public_detail())


@pytest.mark.parametrize("status", [401, 403, 404, 429, 503])
async def test_http_response_does_not_blindly_retry_or_expose_body(monkeypatch, status):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="token=secret")
    tool = mocked_github(monkeypatch, handler)
    with pytest.raises(GitHubToolError) as caught:
        await tool.fetch_file_overview("https://github.com/o/r/pull/1")
    assert len(calls) == 1 and caught.value.upstream_status == status
    assert "secret" not in str(caught.value.public_detail())


async def test_read_cancellation_propagates_without_retry(monkeypatch):
    calls = []
    async def handler(request):
        calls.append(request)
        raise asyncio.CancelledError()
    tool = mocked_github(monkeypatch, handler)
    with pytest.raises(asyncio.CancelledError):
        await tool.fetch_pr("https://github.com/o/r/pull/1")
    assert len(calls) == 1


async def test_total_deadline_interrupts_an_inflight_read(monkeypatch):
    async def handler(request):
        await asyncio.sleep(1)
        return httpx.Response(200, text="too late")
    tool = mocked_github(monkeypatch, handler)
    monkeypatch.setattr("app.tools.github.github_tool.GITHUB_READ_DEADLINE_SECONDS", 0.01)
    with pytest.raises(GitHubToolError) as caught:
        await tool.fetch_diff("https://github.com/o/r/pull/1")
    assert caught.value.code == "github_timeout" and caught.value.api_status == 504
