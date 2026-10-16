"""独立的受限 Publisher 客户端；不暴露给 Review Agent，不自动重试写请求。"""
import re
from urllib.parse import quote

import httpx


class GitHubPublishError(RuntimeError):
    def __init__(self, code, *, uncertain=False, retry_after=None):
        super().__init__(code)
        self.code, self.uncertain, self.retry_after = code, uncertain, retry_after


class GitHubPublisher:
    def __init__(self, token, *, transport=None):
        self._token, self._transport = token, transport

    @staticmethod
    def repo_path(owner, repo):
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) or part in {".", ".."} for part in (owner, repo)):
            raise GitHubPublishError("invalid_repository")
        return f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"

    async def _request(self, method, path, payload=None, *, params=None):
        if not self._token or not path.startswith("/") or path.startswith("//") or ":" in path or ".." in path.split("/"):
            raise GitHubPublishError("invalid_publisher_request")
        write = method != "GET"
        try:
            async with httpx.AsyncClient(base_url="https://api.github.com", timeout=20, follow_redirects=False,
                trust_env=False, transport=self._transport, headers={"Authorization": "Bearer " + self._token,
                "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}) as client:
                response = await client.request(method, path, json=payload, params=params)
        except httpx.TransportError:
            raise GitHubPublishError("github_transport_error", uncertain=write) from None
        if response.status_code >= 300:
            retry_after = None
            if response.status_code in {403, 429} and ("Retry-After" in response.headers or response.headers.get("X-RateLimit-Remaining") == "0"):
                raw = response.headers.get("Retry-After", "60")
                retry_after = max(1, min(int(raw), 3600)) if raw.isdigit() else 60
            code = ("github_rate_limited" if retry_after else "github_not_found" if response.status_code == 404
                    else "github_permission_denied" if response.status_code in {401, 403}
                    else "github_validation_failed" if response.status_code == 422 else "github_request_failed")
            raise GitHubPublishError(code, uncertain=write and response.status_code >= 500, retry_after=retry_after)
        try:
            if len(response.content) > 20_000_000:
                raise ValueError
            value = response.json()
            if not isinstance(value, (dict, list)):
                raise ValueError
            return value
        except ValueError:
            raise GitHubPublishError("github_response_invalid", uncertain=write) from None

    async def get(self, path):
        return await self._request("GET", path)

    async def paginate(self, path):
        output = []
        for page in range(1, 101):
            items = await self._request("GET", path, params={"per_page": 100, "page": page})
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise GitHubPublishError("github_response_invalid")
            output.extend(items)
            if len(items) < 100:
                return output
        raise GitHubPublishError("github_pagination_incomplete")

    async def create_summary(self, path, number, body):
        return await self._request("POST", f"{path}/issues/{number}/comments", {"body": body})

    async def update_summary(self, path, identity, body):
        return await self._request("PATCH", f"{path}/issues/comments/{int(identity)}", {"body": body})

    async def create_review(self, path, number, payload):
        if payload.get("event") != "COMMENT" or not payload.get("body") or not payload.get("comments"):
            raise GitHubPublishError("invalid_review_payload")
        return await self._request("POST", f"{path}/pulls/{number}/reviews", payload)
