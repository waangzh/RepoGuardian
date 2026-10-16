import httpx
import pytest

from app.core.config import Settings
from app.tools.github_publisher import GitHubPublisher, GitHubPublishError
from app.tools.github_diff_mapper import GitHubDiffMapper, MappingFailure
from app.services.publication_preflight import PublicationPreflight
from app.services.publication_planner import PublicationPlanner
from test_publication_planner import publishable_task


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


async def test_preflight_requires_real_identity_permission_and_exact_snapshot():
    task = publishable_task()
    github = GitHubPublisher("fixture", transport=httpx.MockTransport(github_fixture))
    result = await PublicationPreflight(github, publish_config()).check(task)
    assert result["ready"] and result["locations"]["key-a"] == {"path": "src/a.py", "line": 7, "side": "RIGHT"}
    for kwargs, code in [({"head": "c" * 40}, "publication_stale"), ({"permission": False}, "github_permission_denied")]:
        github = GitHubPublisher("fixture", transport=httpx.MockTransport(lambda request: github_fixture(request, **kwargs)))
        with pytest.raises(GitHubPublishError, match=code):
            await PublicationPreflight(github, publish_config()).check(task)
    config = publish_config(repoguardian_github_publish_allowed_repositories="999")
    with pytest.raises(GitHubPublishError, match="repository_not_authorized"):
        await PublicationPreflight(GitHubPublisher("fixture", transport=httpx.MockTransport(github_fixture)), config).check(task)


@pytest.mark.parametrize("side,line,expected", [("head", 7, "RIGHT"), ("base", 7, "LEFT"), ("head", 8, None), ("base", 8, None)])
def test_exact_lines_and_sides(side, line, expected):
    finding = PublicationPlanner().build(publishable_task()).inline_drafts[0]
    finding.anchor.side, finding.anchor.start_line, finding.anchor.end_line = side, line, line
    file = {"filename": "src/a.py", "additions": 1, "deletions": 1, "patch": "@@ -7 +7 @@\n-old\n+new"}
    result = GitHubDiffMapper().map_finding(finding, [file])
    assert isinstance(result, MappingFailure) if expected is None else result["side"] == expected
    file["patch"] = "@@ -7,2 +7,2 @@\n-old\n+new"
    assert isinstance(GitHubDiffMapper().map_finding(finding, [file]), MappingFailure)
    file.update(filename="src/renamed.py", previous_filename="src/a.py", status="renamed", patch="@@ -7 +7 @@\n-old\n+new")
    if side == "base" and line == 7:
        assert GitHubDiffMapper().map_finding(finding, [file])["path"] == "src/renamed.py"


async def test_write_timeout_is_uncertain_and_never_retried_and_errors_are_redacted():
    calls = []
    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("secret response")
    github = GitHubPublisher("secret-token", transport=httpx.MockTransport(timeout))
    with pytest.raises(GitHubPublishError) as caught:
        await github.create_summary("/repos/o/r", 1, "body")
    assert caught.value.uncertain and "secret" not in str(caught.value) and len(calls) == 1


def test_publishing_configuration_defaults_off_and_missing_grants_are_rejected():
    assert not Settings(_env_file=None).repoguardian_github_publish_enabled
    with pytest.raises(ValueError):
        Settings(_env_file=None, repoguardian_github_publish_enabled=True)
