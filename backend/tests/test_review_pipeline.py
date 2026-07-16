import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.agents.providers import LLMProvider
from app.models.review import (
    AgentAction,
    ChangedFile,
    PatchResult,
    PullRequestInfo,
    PullRequestRef,
    ReviewCreateRequest,
    ReviewIssue,
    TaskStatus,
)
from app.services.report_service import ReportService
from app.services.review_service import ReviewService
from app.tools.diff_parser import DiffParser


SAMPLE_PYTHON_REPO = Path(__file__).parent / "fixtures" / "sample_python_repo"


class FakeGitHubTool:
    def __init__(self, pr: PullRequestInfo) -> None:
        self._pr = pr

    async def fetch_pr(self, pr_url: str) -> PullRequestInfo:
        return self._pr


class FakeGitTool:
    def __init__(
        self,
        workspace: Path,
        diff_text: str,
        files: dict[str, str] | None = None,
    ) -> None:
        self._workspace = workspace
        self._diff_text = diff_text
        self._files = files or {}

    def clone_and_diff(self, pr: PullRequestInfo) -> tuple[Path, str]:
        self._workspace.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init"], cwd=self._workspace, check=True, capture_output=True)
        if not self._files and self._diff_text:
            (self._workspace / "sample.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
        for rel_path, content in self._files.items():
            target = self._workspace / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return self._workspace, self._diff_text

    def checkout_sha(self, repo_path: str | Path, sha: str) -> None:
        """此假件只保留一个受控工作树，checkout 本身由集成测试覆盖。"""


class FixtureGitTool:
    """将受版本控制的最小 fixture 复制为审查任务的临时工作树。"""

    def __init__(self, fixture_path: Path, workspace: Path, diff_text: str) -> None:
        self._fixture_path = fixture_path
        self._workspace = workspace
        self._diff_text = diff_text

    def clone_and_diff(self, pr: PullRequestInfo) -> tuple[Path, str]:
        shutil.copytree(self._fixture_path, self._workspace)
        for source_file in self._workspace.rglob("*.py"):
            source_file.write_text(source_file.read_text(encoding="utf-8"), encoding="utf-8")
        subprocess.run(["git", "init"], cwd=self._workspace, check=True, capture_output=True)
        return self._workspace, self._diff_text

    def checkout_sha(self, repo_path: str | Path, sha: str) -> None:
        """fixture 在每个阶段复用同一工作树。"""


class ScriptedProvider(LLMProvider):
    """仅用于图编排测试的确定性 Provider，不属于运行时 Provider。"""

    def __init__(
        self,
        action_sequence: list[AgentAction | dict[str, Any]] | None = None,
        patch_sequence: list[PatchResult | dict[str, Any]] | None = None,
        auto_fixable: bool = False,
    ) -> None:
        self._action_sequence = list(action_sequence or [
            {"action": "review_code", "reason": "测试代码审查"},
        ])
        self._patch_sequence = list(patch_sequence or [])
        self._auto_fixable = auto_fixable

    async def decide(self, state: dict[str, Any], model: str | None) -> AgentAction:
        raw = self._action_sequence.pop(0) if self._action_sequence else {
            "action": "finish_report",
            "reason": "测试动作序列结束",
        }
        return raw if isinstance(raw, AgentAction) else AgentAction.model_validate(raw)

    async def review(
        self,
        pr: PullRequestInfo,
        changed_files: list[ChangedFile],
        diff_text: str,
        model: str | None,
    ) -> list[ReviewIssue]:
        if not changed_files:
            return []
        return [ReviewIssue(
            file_path=changed_files[0].file_path,
            line_no=1,
            severity="low",
            category="maintainability",
            title="测试审查问题",
            description="用于验证审查图状态传递。",
            suggestion="无需修改。",
            confidence=0.2,
            auto_fixable=self._auto_fixable,
        )]

    async def generate_patch(
        self,
        state: dict[str, Any],
        model: str | None,
    ) -> list[PatchResult]:
        if not self._patch_sequence:
            return []
        raw = self._patch_sequence.pop(0)
        return [raw if isinstance(raw, PatchResult) else PatchResult.model_validate(raw)]


class FailingProvider(ScriptedProvider):
    async def review(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("provider should not be called when diff is empty")


@pytest.mark.asyncio
async def test_review_pipeline_with_scripted_provider(tmp_path: Path) -> None:
    diff_text = """diff --git a/sample.py b/sample.py
index 1111111..2222222 100644
--- a/sample.py
+++ b/sample.py
@@ -1,2 +1,3 @@
 def hello():
-    return "hi"
+    name = "RepoGuardian"
+    return f"hi {name}"
"""
    service = _build_service(tmp_path, diff_text, ScriptedProvider())

    task = service.create_task(
        ReviewCreateRequest(pr_url="https://github.com/local/sample/pull/1", model=None)
    )
    await _wait_for_task(service, task.id)
    completed = service.get_task(task.id)

    assert completed is not None
    assert completed.status == TaskStatus.completed, completed.error
    assert completed.changed_files[0].file_path == "sample.py"
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


@pytest.mark.asyncio
async def test_review_pipeline_generates_applies_patch_and_runs_tests(tmp_path: Path) -> None:
    diff_text = """diff --git a/sample.py b/sample.py
index 1111111..2222222 100644
--- a/sample.py
+++ b/sample.py
@@ -1,2 +1,3 @@
 def hello():
-    return "hi"
+    name = "RepoGuardian"
+    return f"hi {name}"
"""
    patch_text = """diff --git a/sample.py b/sample.py
--- a/sample.py
+++ b/sample.py
@@ -1,3 +1,3 @@
 def hello():
     name = "RepoGuardian"
-    return f"hi {name}"
+    return f"hello {name}"
"""
    provider = ScriptedProvider(
        action_sequence=[
            {"action": "review_code", "reason": "先审查 diff"},
            {"action": "abandon_patch", "reason": "验证后结束修复"},
        ],
        patch_sequence=[{"diff_content": patch_text, "status": "generated"}],
        auto_fixable=True,
    )
    service = _build_service(
        tmp_path,
        diff_text,
        provider,
        files={
            "sample.py": 'def hello():\n    name = "RepoGuardian"\n    return f"hi {name}"\n',
            "test_sample.py": 'from sample import hello\n\n\ndef test_hello():\n    assert hello() == "hello RepoGuardian"\n',
        },
    )

    task = service.create_task(
        ReviewCreateRequest(pr_url="https://github.com/local/sample/pull/1", model=None)
    )
    await _wait_for_task(service, task.id)
    completed = service.get_task(task.id)

    assert completed is not None
    assert completed.status == TaskStatus.completed, completed.error
    assert completed.patches
    assert completed.patches[-1].status == "applied"
    assert completed.test_results
    assert completed.test_results[-1].passed is True
    assert completed.validation_snapshots[-1].stage.value == "patched"
    assert completed.validation_snapshots[-1].passed is True
    assert completed.validation_snapshots[-1].patch_id == completed.patches[-1].id
    assert "## 6. 三阶段验证" in completed.report_markdown


@pytest.mark.asyncio
async def test_existing_review_pipeline_with_sample_python_repository(tmp_path: Path) -> None:
    """以静态 fixture 保护准备、审查、修复、验证和报告的完整既有链路。"""
    diff_text = """diff --git a/pricing.py b/pricing.py
index 1111111..2222222 100644
--- a/pricing.py
+++ b/pricing.py
@@ -1,3 +1,3 @@
 def calculate_discounted_total(amount: float, discount_percent: float) -> float:
     \"\"\"Return the total after applying a percentage discount.\"\"\"
-    return amount * (100 - discount_percent) / 100
+    return amount * discount_percent / 100
"""
    patch_text = """diff --git a/pricing.py b/pricing.py
--- a/pricing.py
+++ b/pricing.py
@@ -3 +3 @@
-    return amount * discount_percent / 100
+    return amount * (100 - discount_percent) / 100
"""
    provider = ScriptedProvider(
        action_sequence=[
            {"action": "review_code", "reason": "审查 fixture 中的回归"},
            {"action": "abandon_patch", "reason": "验证后结束修复"},
        ],
        patch_sequence=[{"diff_content": patch_text, "status": "generated"}],
        auto_fixable=True,
    )
    service = ReviewService(
        github_tool=FakeGitHubTool(_build_pr()),
        git_tool=FixtureGitTool(SAMPLE_PYTHON_REPO, tmp_path / "workspace", diff_text),
        diff_parser=DiffParser(),
        provider=provider,
        report_service=ReportService(),
    )

    task = service.create_task(
        ReviewCreateRequest(pr_url="https://github.com/local/sample/pull/1", model=None)
    )
    await _wait_for_task(service, task.id)
    completed = service.get_task(task.id)

    assert completed is not None
    assert completed.status == TaskStatus.completed, completed.error
    assert completed.changed_files[0].file_path == "pricing.py"
    assert completed.issues
    assert completed.patches[-1].status == "applied"
    assert completed.test_results[-1].passed is True
    assert completed.report_markdown is not None


def _build_service(
    tmp_path: Path,
    diff_text: str,
    provider: LLMProvider,
    files: dict[str, str] | None = None,
) -> ReviewService:
    return ReviewService(
        github_tool=FakeGitHubTool(_build_pr()),
        git_tool=FakeGitTool(tmp_path / "workspace", diff_text, files),
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
    # Base、Head 与 Patched 都会运行受控验证，异步图的完成窗口相应扩大。
    for _ in range(400):
        task = service.get_task(task_id)
        if task and task.status in {TaskStatus.completed, TaskStatus.failed}:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("task did not finish")
