"""GitHub API 工具 —— 解析 PR URL 并获取 PR 元数据。"""

import re

import httpx

from app.models.review import PullRequestInfo, PullRequestRef


PR_URL_PATTERN = re.compile(
    r"^https://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/pull/(?P<number>\d+)/?$"
)


class GitHubToolError(RuntimeError):
    """GitHub API 请求失败时抛出。"""
    pass


def parse_pr_url(pr_url: str) -> tuple[str, str, int]:
    """从 GitHub PR URL 中提取 (owner, repo, number)。"""
    match = PR_URL_PATTERN.match(pr_url)
    if not match:
        raise ValueError("请输入合法的 GitHub PR URL，例如 https://github.com/owner/repo/pull/123")
    return match.group("owner"), match.group("repo"), int(match.group("number"))


class GitHubTool:
    """调用 GitHub REST API 获取 PR 详细信息。"""

    def __init__(self, token: str | None = None) -> None:
        self._token = token

    async def fetch_pr(self, pr_url: str) -> PullRequestInfo:
        """获取 PR 的完整元数据（base/head refs, sha, clone_url 等）。"""
        owner, repo, number = parse_pr_url(pr_url)
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url, headers=headers)

        if response.status_code >= 400:
            raise GitHubToolError(f"GitHub API 请求失败：{response.status_code} {response.text[:300]}")

        payload = response.json()
        base_repo = payload["base"]["repo"]
        head_repo = payload["head"]["repo"]
        return PullRequestInfo(
            owner=owner,
            repo=repo,
            number=number,
            title=payload.get("title") or "",
            body=payload.get("body") or None,
            html_url=payload["html_url"],
            clone_url=base_repo["clone_url"],
            base=PullRequestRef(
                ref=payload["base"]["ref"],
                sha=payload["base"]["sha"],
                repo_clone_url=base_repo["clone_url"],
            ),
            head=PullRequestRef(
                ref=payload["head"]["ref"],
                sha=payload["head"]["sha"],
                repo_clone_url=head_repo["clone_url"],
            ),
        )

    async def fetch_diff(self, pr_url: str) -> str:
        """直接获取 PR unified diff，供无克隆、无 LLM 的 Preview 使用。"""
        owner, repo, number = parse_pr_url(pr_url)
        headers = {
            "Accept": "application/vnd.github.v3.diff",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url, headers=headers)
        if response.status_code >= 400:
            raise GitHubToolError(
                f"GitHub diff 请求失败：{response.status_code} {response.text[:300]}"
            )
        return response.text

    async def fetch_file_overview(self, pr_url: str) -> tuple[list[dict], bool]:
        """只取首100项文件的增删元数据，不向概览消费者返回 patch。"""
        owner, repo, number = parse_pr_url(pr_url)
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}/files"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url, headers=headers, params={"per_page": 100, "page": 1})
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list) or len(payload) > 100:
            raise GitHubToolError("invalid_file_overview")
        # 恰好达到页上限时也保守标为不完整；不追下一页而阻塞概览。
        return payload, len(payload) < 100 and "next" not in response.links
