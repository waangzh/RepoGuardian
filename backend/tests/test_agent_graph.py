import shutil
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.agents.providers import LLMProvider
from app.graph.builder import build_review_graph
from app.graph.builder import route_agent_action
from app.models.review import AgentAction, ChangedFile, PatchResult, PullRequestInfo, PullRequestRef, ReviewIssue
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


class GraphScriptedProvider(LLMProvider):
    """为真实 StateGraph 执行提供确定性动作，不发起网络请求。"""

    def __init__(
        self,
        actions: list[dict[str, Any]],
        review_issues: list[ReviewIssue] | None = None,
    ) -> None:
        self._actions = list(actions)
        self._review_issues = review_issues or []
        self.decide_calls = 0
        self.review_calls = 0

    async def decide(self, state: dict[str, Any], model: str | None) -> AgentAction:
        self.decide_calls += 1
        raw = self._actions.pop(0) if self._actions else {
            "action": "finish_report",
            "reason": "动作序列已完成",
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
        return []


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
        return self._workspace, SAMPLE_DIFF


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
    github_tool: object | None = None,
) -> dict[str, Any]:
    return {
        "task_id": "graph-fixture-task",
        "mode": "pr_review",
        "pr_url": "https://github.com/local/sample/pull/1",
        "model": None,
        "_github_tool": github_tool or FixtureGitHubTool(),
        "_git_tool": FixtureGitTool(tmp_path / "workspace"),
        "_diff_parser": DiffParser(),
        "_provider": provider,
    }


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


def test_route_agent_action_uses_next_action() -> None:
    route = route_agent_action({"next_action": {"action": "run_tests"}})

    assert route == "run_tests"


def test_route_agent_action_falls_back_to_report() -> None:
    route = route_agent_action({"next_action": {"action": "unknown"}})

    assert route == "finish_report"


@pytest.mark.asyncio
async def test_graph_routes_normal_action_to_report_and_ends(tmp_path: Path) -> None:
    provider = GraphScriptedProvider([
        {"action": "finish_report", "reason": "正常结束"},
    ])

    result = await build_review_graph().compile().ainvoke(_initial_state(tmp_path, provider))

    assert result["status"] == "completed"
    assert result["next_action"]["action"] == "finish_report"
    assert result["agent_loop_count"] == 1
    assert result["report_markdown"].startswith("# RepoGuardian 代码审查报告")


@pytest.mark.asyncio
async def test_graph_forces_report_after_agent_loop_limit(tmp_path: Path) -> None:
    provider = GraphScriptedProvider([
        {"action": "retrieve_context", "reason": "继续检索"}
        for _ in range(6)
    ])

    result = await build_review_graph().compile().ainvoke(_initial_state(tmp_path, provider))

    assert result["status"] == "completed"
    assert result["next_action"]["action"] == "finish_report"
    assert result["agent_loop_count"] == 7
    assert provider.decide_calls == 6
    assert "Agent loop limit reached" in result["report_markdown"]


@pytest.mark.asyncio
async def test_graph_propagates_node_exception(tmp_path: Path) -> None:
    provider = GraphScriptedProvider([])

    with pytest.raises(RuntimeError, match="fixture repository preparation failed"):
        await build_review_graph().compile().ainvoke(
            _initial_state(tmp_path, provider, FailingGitHubTool())
        )


@pytest.mark.asyncio
async def test_graph_generates_report_directly_when_review_has_no_issues(tmp_path: Path) -> None:
    provider = GraphScriptedProvider([
        {"action": "review_code", "reason": "先确认是否有问题"},
        {"action": "finish_report", "reason": "无问题，直接报告"},
    ])

    result = await build_review_graph().compile().ainvoke(_initial_state(tmp_path, provider))

    assert provider.review_calls == 1
    assert result["review_issues"] == []
    assert result["next_action"]["action"] == "finish_report"
    assert "未发现有明确证据的代码问题" in result["report_markdown"]
