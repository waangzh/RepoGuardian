"""发布相关测试的配置与 GitHub 响应桩。"""

import httpx

from app.core.config import Settings


def publish_config(**updates):
    values = dict(repoguardian_github_publish_enabled=True, repoguardian_github_publish_token="github-fixture",
        repoguardian_publication_api_token="x" * 32, repoguardian_github_publish_publisher_id=7,
        repoguardian_github_publish_allowed_repositories="123", repoguardian_github_publish_pull_requests_write_confirmed=True)
    return Settings(_env_file=None, **(values | updates))


def github_fixture(request, *, head="a" * 40, permission=True, patch=None):
    path = request.url.path
    if path == "/user":
        return httpx.Response(200, json={"id": 7})
    if path == "/repos/o/r":
        return httpx.Response(200, json={"id": 123, "full_name": "o/r", "permissions": {"push": permission}})
    if path.endswith("/files"):
        return httpx.Response(200, json=[{"filename": "src/a.py", "status": "modified", "additions": 1, "deletions": 1,
            "patch": patch if patch is not None else "@@ -7 +7 @@\n-old\n+new"}])
    return httpx.Response(200, json={"number": 1, "state": "open", "merged": False, "changed_files": 1,
        "head": {"sha": head}, "base": {"sha": "b" * 40, "repo": {"id": 123}}})
