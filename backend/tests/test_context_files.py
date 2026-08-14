import subprocess
from pathlib import Path

import pytest

from app.models.review import (
    AgentAction,
    ChangedFile,
    DiffHunk,
    DiffLine,
    ExecutionBudget,
    ReviewToolScope,
    ReviewUnit,
    ReviewUnitComplexity,
)
from app.services.review_unit_executor import ReviewUnitExecutor
from app.tools.context_files import ScopedContextTool, ScopedContextToolError


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "service.ts").write_text(
        "one\ntwo\nthree\nfour\nfive\n", encoding="utf-8"
    )
    (repo / "tests" / "service.test.ts").write_text("test('service', () => {})\n", encoding="utf-8")
    (repo / "secret.ts").write_text("export const SECRET = true\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/service.ts", "tests/service.test.ts", "secret.ts"], cwd=repo, check=True, capture_output=True)
    return repo


def _scope(repo: Path) -> ReviewToolScope:
    return ReviewToolScope(
        review_unit_id="unit-ts",
        commentable_files={"src/service.ts"},
        readable_files={"src/service.ts", "tests/service.test.ts"},
        repository_root=str(repo),
        max_lines_per_read=3,
        max_search_results=2,
    )


@pytest.mark.asyncio
async def test_file_read_is_clamped_and_file_find_stays_in_scope(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    tool = ScopedContextTool()
    scope = _scope(repo)

    snippet = await tool.file_read(
        scope=scope, file_path="src/service.ts", start_line=2, end_line=99
    )
    matches = await tool.file_find(scope=scope, query="service", max_results=20)

    assert snippet["start_line"] == 2
    assert snippet["end_line"] == 4
    assert snippet["content"].startswith("two\nthree\nfour")
    assert snippet["content"].endswith("...(truncated)")
    assert matches == ["src/service.ts", "tests/service.test.ts"]
    with pytest.raises(ScopedContextToolError, match="outside Review Unit"):
        await tool.file_read(scope=scope, file_path="secret.ts")

    sensitive_scope = scope.model_copy(update={"readable_files": {".env"}})
    assert await tool.file_find(scope=sensitive_scope, query="env") == []


@pytest.mark.asyncio
async def test_file_read_diff_uses_parsed_unit_hunks_only(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    scope = _scope(repo).model_copy(update={"max_lines_per_read": 20})
    changed = ChangedFile(
        file_path="src/service.ts",
        change_type="modified",
        additions=1,
        deletions=1,
        hunks=[DiffHunk(
            old_start=2,
            old_length=1,
            new_start=2,
            new_length=1,
            hunk_id="hunk-1",
            lines=[
                DiffLine(kind="deleted", content="two", old_line_no=2),
                DiffLine(kind="added", content="TWO", new_line_no=2),
            ],
        )],
    )

    snippet = await ScopedContextTool().file_read_diff(
        scope=scope,
        file_path="src/service.ts",
        changed_files=[changed],
        hunk_ids=["hunk-1"],
    )

    assert "@@ -2,1 +2,1 @@" in snippet["content"]
    assert "-two" in snippet["content"]
    assert "+TWO" in snippet["content"]
    with pytest.raises(ScopedContextToolError, match="unknown hunk_id"):
        await ScopedContextTool().file_read_diff(
            scope=scope,
            file_path="src/service.ts",
            changed_files=[changed],
            hunk_ids=["missing"],
        )


def test_agent_action_validates_structured_file_tool_requests() -> None:
    action = AgentAction.model_validate({
        "action": "file_read",
        "reason": "读取精确上下文",
        "tool_args": {"request": {
            "file_path": "src/service.ts", "start_line": 2, "end_line": 8
        }},
    })
    assert action.tool_args["request"]["start_line"] == 2

    with pytest.raises(ValueError, match="path traversal"):
        AgentAction.model_validate({
            "action": "file_find",
            "reason": "越界查找",
            "tool_args": {"request": {"query": "../secret"}},
        })


@pytest.mark.asyncio
async def test_unit_executor_routes_direct_file_read_to_scoped_tool(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    scope = _scope(repo)
    unit = ReviewUnit(
        id="unit-ts",
        primary_files=["src/service.ts"],
        related_files=["tests/service.test.ts"],
        estimated_tokens=500,
        complexity=ReviewUnitComplexity.large,
        fingerprint="fingerprint",
        grouping_reason="typescript_unit",
    )
    action = AgentAction.model_validate({
        "action": "file_read",
        "reason": "读取实现",
        "tool_args": {"request": {
            "file_path": "src/service.ts", "start_line": 1, "end_line": 2
        }},
    })
    executor = ReviewUnitExecutor(object(), concurrency=1, timeout_seconds=2)  # type: ignore[arg-type]

    result = await executor._execute_read_tool_node({
        "unit": unit,
        "scope": scope,
        "unit_files": [],
        "next_action": action,
        "budget": ExecutionBudget(max_context_retrievals=2),
        "context": [],
        "tool_events": [],
        "retrieval_history": [],
        "retrieval_no_new_rounds": 0,
    })

    assert result["context"][0]["content"].startswith("one\ntwo")
    assert result["tool_events"][0].tool == "file_read"
    assert result["budget"].context_retrievals == 1
