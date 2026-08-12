import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.agents.providers import LLMProvider, OpenAICompatibleProvider
from app.graph.policies import UNIT_ACTION_REGISTRY, UNIT_ACTION_ROUTES
from app.models.review import (
    AgentAction,
    ChangedFile,
    DiffHunk,
    PatchResult,
    PullRequestInfo,
    PullRequestRef,
    ReviewIssue,
    ReviewUnit,
    ReviewUnitComplexity,
)
from app.services.review_unit_executor import ReviewUnitExecutor


class MultiRoundProvider(LLMProvider):
    def __init__(self) -> None:
        self.decision_states: list[dict[str, Any]] = []
        self.actions = [
            AgentAction.model_validate({
                "action": "retrieve_context",
                "reason": "读取常量",
                "tool_args": {"plan": {
                    "reason": "定位 FEATURE",
                    "target_files": ["module.py"],
                    "search_terms": ["FEATURE"],
                    "relevance_types": ["text"],
                }},
            }),
            AgentAction.model_validate({
                "action": "retrieve_context",
                "reason": "读取返回值",
                "tool_args": {"plan": {
                    "reason": "定位 return",
                    "target_files": ["module.py"],
                    "search_terms": ["return"],
                    "relevance_types": ["text"],
                }},
            }),
            AgentAction(action="report_issue", reason="证据充分，报告问题"),
            AgentAction(action="task_done", reason="Unit 审查完成"),
        ]

    async def decide(self, state: dict[str, Any], model: str | None) -> AgentAction:
        self.decision_states.append(state)
        OpenAICompatibleProvider._build_decision_prompt(state)
        return self.actions.pop(0)

    async def review(
        self,
        pr: PullRequestInfo,
        changed_files: list[ChangedFile],
        diff_text: str,
        model: str | None,
    ) -> list[ReviewIssue]:
        return [ReviewIssue(
            review_unit_id="pending",
            severity="low",
            category="correctness",
            title="示例问题",
            confidence=0.8,
            affected_behavior="功能开关行为不一致",
            failure_scenario="启用功能时返回旧值",
            recommendation="统一返回值",
            primary_evidence={"file_path": "module.py", "existing_code": "    return FEATURE"},
        )]

    async def generate_patch(
        self, state: dict[str, Any], model: str | None
    ) -> list[PatchResult]:
        return []


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    (repo / "module.py").write_text(
        "FEATURE = True\n\ndef value():\n    return FEATURE\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "module.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=test@example.test", "-c", "user.name=Test",
         "commit", "-m", "fixture"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


@pytest.mark.asyncio
async def test_per_unit_langgraph_supports_bounded_read_rounds_and_explicit_done(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    changed = ChangedFile(
        file_path="module.py",
        change_type="modified",
        additions=1,
        deletions=1,
        hunks=[DiffHunk(
            old_start=4,
            old_length=1,
            new_start=4,
            new_length=1,
            added_lines=[{"line_no": 4, "content": "    return FEATURE"}],
        )],
    )
    unit = ReviewUnit(
        id="unit-large",
        primary_files=["module.py"],
        related_files=[],
        diff_hunk_ids=[],
        changed_symbols=["value"],
        rule_ids=["review.general"],
        risk_tags=[],
        estimated_tokens=1_000,
        complexity=ReviewUnitComplexity.large,
        fingerprint="fingerprint",
        grouping_reason="single_file",
    )
    pr = PullRequestInfo(
        owner="local",
        repo="sample",
        number=1,
        title="PR",
        html_url="https://example.test/pr/1",
        clone_url="https://example.test/repo.git",
        base=PullRequestRef(ref="main", sha="base", repo_clone_url="https://example.test/repo.git"),
        head=PullRequestRef(ref="feature", sha="head", repo_clone_url="https://example.test/repo.git"),
    )
    provider = MultiRoundProvider()
    executor = ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=5)
    result = await executor.execute_unit(unit, {
        "task_id": "task",
        "pr_info": pr.model_dump(mode="json"),
        "changed_files": [changed.model_dump(mode="json")],
        "file_index": [{"path": "module.py", "language": "python", "imports": []}],
        "symbol_index": [{
            "file": "module.py", "symbol": "value", "type": "function",
            "start_line": 3, "end_line": 4, "calls": [],
        }],
        "repo_path": str(repo),
    })

    graph_nodes = set(executor.unit_graph.get_graph().nodes)
    assert {"prepare_unit", "plan_unit", "agent_decide", "execute_read_tool",
            "report_issue", "collect_issue", "finish_unit"} <= graph_nodes
    assert result.status.value == "completed"
    assert result.execution_budget.context_retrievals == 2
    assert result.issues and result.issues[0].review_unit_id == unit.id
    assert result.messages[-1].action == "task_done"
    assert provider.decision_states[0]["retrieval_history"] == []
    assert provider.decision_states[1]["retrieval_history"][0]["status"] == "completed"
    assert provider.decision_states[1]["retrieval_history"][0]["new_snippet_count"] > 0


def test_unit_graph_routes_are_generated_from_action_registry() -> None:
    assert UNIT_ACTION_ROUTES == {
        item.action.value: item.route for item in UNIT_ACTION_REGISTRY
    }
    assert UNIT_ACTION_ROUTES["request_human"] == "finish_unit"
