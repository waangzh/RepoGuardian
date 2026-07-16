import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.agents.providers import LLMProvider
from app.graph.policies import ALLOWED_ACTIONS_BY_PHASE
from app.graph.review_graph import build_review_graph
from app.graph.routers import route_discovery_action, route_repair_action
from app.models.review import (
    AgentAction,
    ChangedFile,
    ExecutionBudget,
    PatchResult,
    PullRequestInfo,
    PullRequestRef,
    ReviewIssue,
    ReviewPhase,
)
from app.tools.diff_parser import DiffParser


SAMPLE_PYTHON_REPO = Path(__file__).parent / "fixtures" / "sample_python_repo"
SAMPLE_DIFF = """diff --git a/pricing.py b/pricing.py
index 1111111..2222222 100644
--- a/pricing.py
+++ b/pricing.py
@@ -1,3 +1,3 @@
 def calculate_discounted_total(amount: float, discount_percent: float) -> float:
     \"\"\"Return the total after applying a percentage discount.\"\"\"
-    return amount * (100 - discount_percent) / 100
+    return amount * discount_percent / 100
"""
SAMPLE_PATCH = """diff --git a/pricing.py b/pricing.py
--- a/pricing.py
+++ b/pricing.py
@@ -1,3 +1,3 @@
 def calculate_discounted_total(amount: float, discount_percent: float) -> float:
     \"\"\"Return the total after applying a percentage discount.\"\"\"
-    return amount * discount_percent / 100
+    return amount * (100 - discount_percent) / 100
"""
SECOND_SAMPLE_PATCH = """diff --git a/verification-note.txt b/verification-note.txt
new file mode 100644
--- /dev/null
+++ b/verification-note.txt
@@ -0,0 +1 @@
+validated
"""
INVALID_SAMPLE_PATCH = """diff --git a/pricing.py b/pricing.py
--- a/pricing.py
+++ b/pricing.py
@@ -20,1 +20,1 @@
-missing source line
+replacement
"""


class GraphScriptedProvider(LLMProvider):
    """用于图测试的确定性 Provider，不访问网络或宿主机凭据。"""

    def __init__(
        self,
        actions: list[dict[str, Any]] | None = None,
        review_issues: list[ReviewIssue] | None = None,
        patches: list[PatchResult] | None = None,
    ) -> None:
        self._actions = list(actions or [{"action": "review_code", "reason": "开始诊断"}])
        self._review_issues = review_issues or []
        self._patches = list(patches or [])
        self.decide_calls = 0
        self.review_calls = 0
        self.patch_calls = 0

    async def decide(self, state: dict[str, Any], model: str | None) -> AgentAction:
        self.decide_calls += 1
        raw = self._actions.pop(0) if self._actions else {
            "action": "abandon_patch",
            "reason": "脚本化动作已完成",
        }
        return AgentAction.model_validate(raw)

    async def review(
        self,
        pr: PullRequestInfo,
        changed_files: list[ChangedFile],
        diff_text: str,
        model: str | None,
    ) -> list[ReviewIssue]:
        self.review_calls += 1
        return self._review_issues

    async def generate_patch(
        self,
        state: dict[str, Any],
        model: str | None,
    ) -> list[PatchResult]:
        self.patch_calls += 1
        return list(self._patches)


class FixtureGitHubTool:
    async def fetch_pr(self, pr_url: str) -> PullRequestInfo:
        return _sample_pr()


class FailingGitHubTool:
    async def fetch_pr(self, pr_url: str) -> PullRequestInfo:
        raise RuntimeError("fixture repository preparation failed")


class FixtureGitTool:
    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    def clone_and_diff(self, pr: PullRequestInfo) -> tuple[Path, str]:
        shutil.copytree(SAMPLE_PYTHON_REPO, self._workspace)
        for source_file in self._workspace.rglob("*.py"):
            source_file.write_text(
                source_file.read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
            )
        subprocess.run(["git", "init"], cwd=self._workspace, check=True, capture_output=True)
        return self._workspace, SAMPLE_DIFF

    def checkout_sha(self, repo_path: str | Path, sha: str) -> None:
        """图测试只验证 checkout 顺序；工作树内容由 fixture 固定提供。"""


def _sample_pr() -> PullRequestInfo:
    ref = PullRequestRef(
        ref="main",
        sha="1111111111111111111111111111111111111111",
        repo_clone_url="https://github.com/local/sample.git",
    )
    return PullRequestInfo(
        owner="local",
        repo="sample",
        number=1,
        title="Fixture graph review",
        html_url="https://github.com/local/sample/pull/1",
        clone_url="https://github.com/local/sample.git",
        base=ref,
        head=ref,
    )


def _initial_state(
    tmp_path: Path,
    provider: LLMProvider,
    *,
    budget: ExecutionBudget | None = None,
    github_tool: object | None = None,
) -> dict[str, Any]:
    return {
        "task_id": "graph-fixture-task",
        "mode": "pr_review",
        "pr_url": "https://github.com/local/sample/pull/1",
        "model": None,
        "execution_budget": (budget or ExecutionBudget()).model_dump(),
        "_github_tool": github_tool or FixtureGitHubTool(),
        "_git_tool": FixtureGitTool(tmp_path / "workspace"),
        "_diff_parser": DiffParser(),
        "_provider": provider,
    }


def _auto_fixable_issue() -> ReviewIssue:
    return ReviewIssue(
        id="discount-fix",
        file_path="pricing.py",
        line_no=3,
        severity="high",
        category="correctness",
        title="折扣计算错误",
        description="折扣百分比被直接作为剩余金额使用。",
        suggestion="使用 100 - discount_percent。",
        confidence=0.95,
        auto_fixable=True,
    )


def test_agent_action_accepts_valid_json_shape() -> None:
    action = AgentAction.model_validate({
        "action": "retrieve_context",
        "reason": "需要更多上下文",
        "target_issue_ids": [],
        "tool_args": {},
    })

    assert action.action == "retrieve_context"


def test_agent_action_rejects_unknown_action() -> None:
    with pytest.raises(ValidationError):
        AgentAction.model_validate({"action": "unknown", "reason": "bad"})


def test_legal_actions_are_routed_only_within_their_phase() -> None:
    assert ALLOWED_ACTIONS_BY_PHASE[ReviewPhase.discovery] == {
        "retrieve_context",
        "review_code",
    }
    assert ALLOWED_ACTIONS_BY_PHASE[ReviewPhase.repair] == {
        "revise_patch",
        "abandon_patch",
    }
    assert route_discovery_action({"next_action": {"action": "retrieve_context", "reason": "x"}}) == (
        "context_retrieve"
    )
    assert route_repair_action({"next_action": {"action": "revise_patch", "reason": "x"}}) == (
        "generate_patch"
    )


@pytest.mark.asyncio
async def test_illegal_phase_action_is_rejected_and_cannot_run_tool(tmp_path: Path) -> None:
    provider = GraphScriptedProvider(actions=[{"action": "run_tests", "reason": "越权动作"}])

    result = await build_review_graph().compile().ainvoke(_initial_state(tmp_path, provider))

    assert result["status"] == "completed"
    assert provider.review_calls == 1
    assert not result.get("test_results")
    assert any(event["status"] == "rejected" for event in result["agent_events"])


@pytest.mark.asyncio
async def test_context_budget_exhaustion_skips_extra_retrieval(tmp_path: Path) -> None:
    provider = GraphScriptedProvider(actions=[{"action": "retrieve_context", "reason": "继续检索"}])
    budget = ExecutionBudget(max_context_retrievals=1)

    result = await build_review_graph().compile().ainvoke(_initial_state(tmp_path, provider, budget=budget))

    assert result["execution_budget"]["context_retrievals"] == 1
    assert provider.decide_calls == 0
    assert result["next_action"]["action"] == "review_code"


@pytest.mark.asyncio
async def test_patch_budget_exhaustion_skips_repair_subgraph_work(tmp_path: Path) -> None:
    provider = GraphScriptedProvider(review_issues=[_auto_fixable_issue()])
    budget = ExecutionBudget(max_patch_attempts=0)

    result = await build_review_graph().compile().ainvoke(_initial_state(tmp_path, provider, budget=budget))

    assert result["status"] == "completed"
    assert provider.patch_calls == 0
    assert not result.get("patches")
    assert result["report_markdown"]


@pytest.mark.asyncio
async def test_model_call_budget_exhaustion_still_publishes_report(tmp_path: Path) -> None:
    provider = GraphScriptedProvider()
    budget = ExecutionBudget(max_model_calls=0)

    result = await build_review_graph().compile().ainvoke(_initial_state(tmp_path, provider, budget=budget))

    assert result["status"] == "completed"
    assert result["execution_budget"]["model_calls"] == 0
    assert provider.decide_calls == provider.review_calls == 0
    assert result["report_markdown"]


@pytest.mark.asyncio
async def test_main_flow_cannot_skip_report(tmp_path: Path) -> None:
    result = await build_review_graph().compile().ainvoke(
        _initial_state(tmp_path, GraphScriptedProvider())
    )

    assert result["phase"] == ReviewPhase.completed
    assert result["step_progress"][-1]["node"] == "report"
    assert result["report_markdown"].startswith("# RepoGuardian")


@pytest.mark.asyncio
async def test_repair_subgraph_returns_to_main_flow(tmp_path: Path) -> None:
    provider = GraphScriptedProvider(
        actions=[
            {"action": "review_code", "reason": "开始诊断"},
            {"action": "abandon_patch", "reason": "验证后结束修复"},
        ],
        review_issues=[_auto_fixable_issue()],
        patches=[PatchResult(issue_id="discount-fix", diff_content=SAMPLE_PATCH)],
    )

    result = await build_review_graph().compile().ainvoke(_initial_state(tmp_path, provider))

    assert provider.patch_calls == 1
    assert result["patches"][-1]["status"] == "applied"
    assert result["test_results"][-1]["passed"] is True
    assert result["step_progress"][-1]["node"] == "report"


@pytest.mark.asyncio
async def test_repair_subgraph_applies_and_verifies_every_generated_patch(tmp_path: Path) -> None:
    first_patch = PatchResult(issue_id="discount-fix", diff_content=SAMPLE_PATCH)
    second_patch = PatchResult(issue_id="verification-note", diff_content=SECOND_SAMPLE_PATCH)
    provider = GraphScriptedProvider(
        actions=[
            {"action": "review_code", "reason": "开始诊断"},
            {"action": "abandon_patch", "reason": "所有候选补丁已验证"},
        ],
        review_issues=[_auto_fixable_issue()],
        patches=[first_patch, second_patch],
    )

    result = await build_review_graph().compile().ainvoke(_initial_state(tmp_path, provider))

    assert provider.patch_calls == 1
    assert [patch["status"] for patch in result["patches"]] == ["applied", "applied"]
    patched_snapshots = [
        snapshot for snapshot in result["validation_snapshots"] if snapshot["stage"] == "patched"
    ]
    assert [snapshot["patch_id"] for snapshot in patched_snapshots] == [first_patch.id, second_patch.id]


@pytest.mark.asyncio
async def test_failed_current_patch_does_not_reuse_previous_patch_validation(tmp_path: Path) -> None:
    first_patch = PatchResult(issue_id="discount-fix", diff_content=SAMPLE_PATCH)
    failed_patch = PatchResult(issue_id="invalid", diff_content=INVALID_SAMPLE_PATCH)
    provider = GraphScriptedProvider(
        actions=[{"action": "review_code", "reason": "开始诊断"}],
        review_issues=[_auto_fixable_issue()],
        patches=[first_patch, failed_patch],
    )

    result = await build_review_graph().compile().ainvoke(_initial_state(tmp_path, provider))

    assert [patch["status"] for patch in result["patches"]] == ["applied", "apply_failed"]
    patched_snapshots = [
        snapshot for snapshot in result["validation_snapshots"] if snapshot["stage"] == "patched"
    ]
    assert [snapshot["patch_id"] for snapshot in patched_snapshots] == [first_patch.id]
    assert result["active_patch_id"] == failed_patch.id


@pytest.mark.asyncio
async def test_graph_propagates_repository_preparation_failure(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="fixture repository preparation failed"):
        await build_review_graph().compile().ainvoke(
            _initial_state(tmp_path, GraphScriptedProvider(), github_tool=FailingGitHubTool())
        )
