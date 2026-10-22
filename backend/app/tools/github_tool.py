"""GitHub API 工具 —— 解析 PR URL 并获取 PR 元数据。"""

import re
import asyncio

import httpx

from app.models.review import PullRequestInfo, PullRequestRef


PR_URL_PATTERN = re.compile(
    r"^https://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/pull/(?P<number>\d+)/?$"
)
GITHUB_READ_DEADLINE_SECONDS = 45


class GitHubToolError(RuntimeError):
    """稳定的上游错误协议；不保存响应正文或原始网络异常文本。"""
    def __init__(self, code="github_response_invalid", *, upstream_status=None, retryable=False):
        self.code = code
        self.upstream_status = upstream_status
        self.retryable = retryable
        messages = {
            "github_connection_failed": "连接 GitHub 失败，请检查网络或代理后重试。",
            "github_timeout": "读取 GitHub 超时，请检查网络或代理后重试。",
            "github_response_invalid": "GitHub 返回的数据无效，请稍后重试。",
            "github_http_error": {401: "GitHub 身份验证失败，请检查服务端凭据。",
                403: "GitHub 拒绝访问，请检查仓库权限和 API 配额。",
                404: "GitHub PR 不存在或当前身份无权访问。",
                429: "GitHub 请求过于频繁，请稍后重试。"}.get(upstream_status, "GitHub 服务暂不可用，请稍后重试。"),
        }
        super().__init__(messages.get(code, messages["github_response_invalid"]))

    @property
    def api_status(self):
        return 504 if self.code == "github_timeout" else 503 if self.retryable else 502

    def public_detail(self):
        return {"code": self.code, "message": str(self), "retryable": self.retryable,
                "upstream_status": self.upstream_status}


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

    async def _get(self, url, headers, *, params=None):
        """只读GET最多3次，包含退避在内总计45秒；继续使用既有代理/TLS设置。"""
        try:
            async with asyncio.timeout(GITHUB_READ_DEADLINE_SECONDS):
                async with httpx.AsyncClient(timeout=30) as client:
                    for attempt in range(3):
                        try:
                            response = await client.get(url, headers=headers, **({"params": params} if params else {}))
                        except httpx.TimeoutException:
                            error = GitHubToolError("github_timeout", retryable=True)
                        except (httpx.NetworkError, httpx.ProxyError, httpx.RemoteProtocolError):
                            error = GitHubToolError("github_connection_failed", retryable=True)
                        except httpx.RequestError:
                            raise GitHubToolError("github_connection_failed") from None
                        else:
                            if response.status_code >= 400:
                                raise GitHubToolError("github_http_error", upstream_status=response.status_code,
                                    retryable=response.status_code == 429 or response.status_code >= 500)
                            return response
                        if attempt == 2:
                            raise error from None
                        await asyncio.sleep(0.25 * 2 ** attempt)
        except TimeoutError:
            raise GitHubToolError("github_timeout", retryable=True) from None

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
        response = await self._get(url, headers)
        try:
            return self._parse_pr(response.json(), owner, repo, number)
        except (ValueError, KeyError, TypeError):
            raise GitHubToolError() from None

    async def fetch_discussions(self, pr_url: str, *, task_id: str, head_sha: str, base_sha: str):
        from app.tools.github_discussions import GitHubDiscussionReader
        owner, repo, number = parse_pr_url(pr_url)
        return await GitHubDiscussionReader(self).collect(task_id=task_id, pr_url=pr_url,
            head_sha=head_sha, base_sha=base_sha, owner=owner, repo=repo, number=number)

    def _parse_pr(self, payload, owner, repo, number):
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

    def compare_delta(self, owner: str, repo: str, base_sha: str, head_sha: str) -> dict | None:
        """同步只读比较两个固定提交；返回 status 与变化路径，不返回 patch 正文。

        GitHub compare 最多返回 300 个文件；达到上限或结构不符时返回 None，
        调用方必须退回完整审查，不能把截断列表当作完整增量。调用方应在
        工作线程中执行，避免阻塞事件循环。
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        url = f"https://api.github.com/repos/{owner}/{repo}/compare/{base_sha}...{head_sha}"
        try:
            with httpx.Client(timeout=30) as client:
                response = client.get(url, headers=headers)
        except (httpx.TimeoutException, httpx.RequestError):
            return None
        if response.status_code >= 400:
            return None
        try:
            payload = response.json()
            files = payload["files"]
            status = payload.get("status")
            if (not isinstance(files, list) or len(files) >= 300
                    or status not in {"ahead", "behind", "diverged", "identical"}):
                return None
            if any(not isinstance(item, dict) or not isinstance(item.get("filename"), str)
                   or not item["filename"] for item in files):
                return None
            paths = {item["filename"] for item in files if isinstance(item, dict)
                     and isinstance(item.get("filename"), str)}
            paths.update(item["previous_filename"] for item in files if isinstance(item, dict)
                         and isinstance(item.get("previous_filename"), str))
            return {"status": status, "files": sorted(paths)}
        except (ValueError, KeyError, TypeError):
            return None

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
        response = await self._get(url, headers)
        return response.text

    async def fetch_file_overview(self, pr_url: str) -> tuple[list[dict], bool]:
        """只取首100项文件的增删元数据，不向概览消费者返回 patch。"""
        owner, repo, number = parse_pr_url(pr_url)
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}/files"
        response = await self._get(url, headers, params={"per_page": 100, "page": 1})
        try:
            payload = response.json()
        except ValueError:
            raise GitHubToolError() from None
        if not isinstance(payload, list) or len(payload) > 100:
            raise GitHubToolError()
        # 恰好达到页上限时也保守标为不完整；不追下一页而阻塞概览。
        return payload, len(payload) < 100 and "next" not in response.links
