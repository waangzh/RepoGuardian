"""审查服务测试夹具：真实服务装配与预览确认请求。"""

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.review import PullRequestInfo, ReviewCreateRequest
from app.services.review.report_service import ReportService
from app.services.review.review_service import ReviewService
from app.tools.diff_parser import DiffParser
from app.tools.git_tool import GitTool


@pytest.fixture
def plan_service(production_input, persistence, monkeypatch):
    state, provider, calls = production_input
    configuration = copy.deepcopy(state["task_runtime_configuration"])
    configuration["call_resolution_version"] = "import-evidence-v5"
    monkeypatch.setattr("app.review.runtime_spec.task_runtime_configuration", lambda *a, **k: copy.deepcopy(configuration))
    monkeypatch.setattr("app.services.review.review_service._cleanup_repo", AsyncMock(return_value=True))
    pr = PullRequestInfo(owner="o", repo="r", number=1, title="fixture", body="", clone_url="https://github.com/o/r.git",
        html_url="https://github.com/o/r/pull/1",
        head={"sha": state["head_sha"], "ref": "feature", "repo_clone_url": "https://github.com/o/r.git"},
        base={"sha": state["base_sha"], "ref": "main", "repo_clone_url": "https://github.com/o/r.git"})
    state["pr_info"] = pr.model_dump(mode="json")
    git = GitTool()
    raw = git._run([git._git, "-C", state["repo_path"], "diff", "--unified=3", state["base_sha"], state["head_sha"]])
    git.clone_and_diff = lambda *a, **k: (state["repo_path"], raw)
    repository, queue, _ = persistence
    service = ReviewService(SimpleNamespace(fetch_pr=AsyncMock(return_value=pr)), git, DiffParser(), provider,
                            ReportService(), repository=repository, task_queue=queue)
    service._ensure_worker_started = lambda: None
    return service, state, provider, calls, pr


def confirmation(preview):
    return ReviewCreateRequest(pr_url="https://github.com/o/r/pull/1", preview_id=preview.preview_id,
        plan_hash=preview.plan_hash, expected_head_sha=preview.snapshot.head_sha,
        expected_effective_base_sha=preview.snapshot.effective_base_sha)
