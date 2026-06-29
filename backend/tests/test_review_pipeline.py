import asyncio
from pathlib import Path

import pytest

from app.agents.providers import MockProvider
from app.models.review import PullRequestInfo, PullRequestRef, ReviewCreateRequest, TaskStatus
from app.services.report_service import ReportService
from app.services.review_service import ReviewService
from app.tools.diff_parser import DiffParser


class FakeGitHubTool:
    def __init__(self, pr: PullRequestInfo) -> None:
        self._pr = pr

    async def fetch_pr(self, pr_url: str) -> PullRequestInfo:
        return self._pr


class FakeGitTool:
    def __init__(self, workspace: Path, diff_text: str) -> None:
        self._workspace = workspace
        self._diff_text = diff_text

    def clone_and_diff(self, pr: PullRequestInfo) -> tuple[Path, str]:
        self._workspace.mkdir(parents=True, exist_ok=True)
        return self._workspace, self._diff_text


class FailingProvider(MockProvider):
    async def review(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("provider should not be called when diff is empty")


@pytest.mark.asyncio
async def test_review_pipeline_with_mock_provider(tmp_path: Path) -> None:
    diff_text = """diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -1,2 +1,3 @@
 def hello():
-    return "hi"
+    name = "RepoGuardian"
+    return f"hi {name}"
"""
    service = _build_service(tmp_path, diff_text, MockProvider())

    task = service.create_task(
        ReviewCreateRequest(pr_url="https://github.com/local/sample/pull/1", model=None)
    )
    await _wait_for_task(service, task.id)
    completed = service.get_task(task.id)

    assert completed is not None
    assert completed.status == TaskStatus.completed, completed.error
    assert completed.changed_files[0].file_path == "app.py"
    assert completed.issues
    assert completed.report_markdown is not None


@pytest.mark.asyncio
async def test_review_pipeline_skips_llm_when_diff_is_empty(tmp_path: Path) -> None:
    service = _build_service(tmp_path, "", FailingProvider())

    task = service.create_task(
        ReviewCreateRequest(pr_url="https://github.com/local/sample/pull/1", model=None)
    )
    await _wait_for_task(service, task.id)
    completed = service.get_task(task.id)

    assert completed is not None
    assert completed.status == TaskStatus.completed, completed.error
    assert completed.changed_files == []
    assert completed.issues == []
    assert completed.report_markdown is not None


def _build_service(tmp_path: Path, diff_text: str, provider: MockProvider) -> ReviewService:
    return ReviewService(
        github_tool=FakeGitHubTool(_build_pr()),
        git_tool=FakeGitTool(tmp_path / "workspace", diff_text),
        diff_parser=DiffParser(),
        provider=provider,
        report_service=ReportService(),
    )


def _build_pr() -> PullRequestInfo:
    return PullRequestInfo(
        owner="local",
        repo="sample",
        number=1,
        title="Local test PR",
        html_url="https://github.com/local/sample/pull/1",
        clone_url="https://github.com/local/sample.git",
        base=PullRequestRef(
            ref="master",
            sha="1111111111111111111111111111111111111111",
            repo_clone_url="https://github.com/local/sample.git",
        ),
        head=PullRequestRef(
            ref="feature",
            sha="2222222222222222222222222222222222222222",
            repo_clone_url="https://github.com/local/sample.git",
        ),
    )


async def _wait_for_task(service: ReviewService, task_id: str) -> None:
    for _ in range(100):
        task = service.get_task(task_id)
        if task and task.status in {TaskStatus.completed, TaskStatus.failed}:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("task did not finish")