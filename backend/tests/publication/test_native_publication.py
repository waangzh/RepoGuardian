import pytest

from app.services.publication.publication_preflight import PublicationPreflight
from app.tools.github.github_publisher import GitHubPublishError


class Config:
    repoguardian_github_publish_enabled = True
    repoguardian_github_publish_publisher_id = 7
    repoguardian_github_publish_allowed_repositories = "123"
    repoguardian_github_publish_pull_requests_write_confirmed = True
    repoguardian_github_publish_max_inline = 10


class GitHub:
    def __init__(self, run):
        self.head = run.snapshot.head_sha
        self.base = run.snapshot.declared_base_sha
        self.permission = True

    def repo_path(self, owner, repo):
        return f"/repos/{owner}/{repo}"

    async def get(self, path):
        if path == "/user":
            return {"id": 7}
        if path == "/repos/local/repo":
            return {"id": 123, "full_name": "local/repo", "permissions": {"push": self.permission}}
        if path == "/repos/local/repo/pulls/1":
            return {"number": 1, "state": "open", "merged": False, "changed_files": 1,
                "head": {"sha": self.head}, "base": {"sha": self.base, "repo": {"id": 123}}}
        raise AssertionError(path)

    async def paginate(self, path):
        assert path == "/repos/local/repo/pulls/1/files"
        return [{"filename": "safe.py", "status": "modified", "additions": 1, "deletions": 1,
            "patch": "@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2"}]


async def test_preflight_keeps_identity_permission_and_precise_right_mapping(normalized_run):
    run, artifacts = normalized_run
    checked = await PublicationPreflight(GitHub(run), Config(), artifacts).check(run)
    assert checked["report_version"] == checked["plan"].report_version
    assert checked["locations"]["finding-candidate"] == {"path": "safe.py", "line": 1, "side": "RIGHT"}


async def test_preflight_rejects_stale_head_and_permission(normalized_run):
    run, artifacts = normalized_run
    github = GitHub(run)
    github.head = "0" * 40
    with pytest.raises(GitHubPublishError, match="publication_stale"):
        await PublicationPreflight(github, Config(), artifacts).check(run)
    github.head, github.permission = run.snapshot.head_sha, False
    with pytest.raises(GitHubPublishError, match="github_permission_denied"):
        await PublicationPreflight(github, Config(), artifacts).check(run)
