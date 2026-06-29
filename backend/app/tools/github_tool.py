import re

import httpx

from app.models.review import PullRequestInfo, PullRequestRef


PR_URL_PATTERN = re.compile(
    r"^https://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/pull/(?P<number>\d+)/?$"
)


class GitHubToolError(RuntimeError):
    pass


def parse_pr_url(pr_url: str) -> tuple[str, str, int]:
    match = PR_URL_PATTERN.match(pr_url)
    if not match:
        raise ValueError("请输入合法的 GitHub PR URL，例如 https://github.com/owner/repo/pull/123")
    return match.group("owner"), match.group("repo"), int(match.group("number"))


class GitHubTool:
    def __init__(self, token: str | None = None) -> None:
        self._token = token

    async def fetch_pr(self, pr_url: str) -> PullRequestInfo:
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

