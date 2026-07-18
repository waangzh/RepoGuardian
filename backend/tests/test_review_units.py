import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.agents.providers import LLMProvider
from app.graph.nodes.report import complete_node
from app.graph.nodes.review_units import review_units_node
from app.models.review import (
    AgentAction,
    ChangedFile,
    PatchResult,
    PullRequestInfo,
    PullRequestRef,
    ReviewIssue,
    ReviewPreviewRequest,
    ReviewUnit,
    ReviewUnitComplexity,
    ReviewUnitResult,
    ReviewUnitStatus,
)
from app.services.report_service import ReportService
from app.services.review_planner import DeterministicReviewPlanner
from app.services.review_service import ReviewService
from app.services.review_unit_executor import ReviewUnitExecutor
from app.tools.code_search import CodeSearchTool, ContextRetrievalPlanError
from app.tools.diff_parser import DiffParser


def _parse(*sections: str) -> list[ChangedFile]:
    return DiffParser().parse("\n".join(sections))


def _diff(path: str, body: str = "+new\n-old", *, old: str | None = None) -> str:
    old_path = old or path
    lines = body.splitlines()
    removed = sum(line.startswith("-") for line in lines)
    added = sum(line.startswith("+") for line in lines)
    hunk = "\n".join(lines)
    return (
        f"diff --git a/{old_path} b/{path}\n"
        f"--- a/{old_path}\n+++ b/{path}\n"
        f"@@ -1,{max(removed, 1)} +1,{max(added, 1)} @@\n{hunk}"
    )


def test_single_file_pr_generates_one_stable_unit() -> None:
    planner = DeterministicReviewPlanner()
    files = _parse(_diff("src/value.py", "-value = 1\n+value = 2"))
    first = planner.plan(files, base_sha="base", head_sha="head")
    second = planner.plan(files, base_sha="base", head_sha="head")

    assert len(first.review_units) == 1
    assert first.review_units[0].primary_files == ["src/value.py"]
    assert first.review_units[0].id == second.review_units[0].id
    assert first.review_units[0].fingerprint == second.review_units[0].fingerprint


def test_implementation_and_test_merge_but_unrelated_file_does_not() -> None:
    files = _parse(
        _diff("src/foo.py"),
        _diff("tests/test_foo.py"),
        _diff("src/bar.py"),
    )
    plan = DeterministicReviewPlanner().plan(files, base_sha="b", head_sha="h")
    groups = {tuple(unit.primary_files): unit.grouping_reason for unit in plan.review_units}

    assert groups[("src/foo.py", "tests/test_foo.py")] == "implementation_with_tests"
    assert groups[("src/bar.py",)] == "single_file"
    all_primary = [path for unit in plan.review_units for path in unit.primary_files]
    assert len(all_primary) == len(set(all_primary))


def test_deletion_and_lockfile_receive_dedicated_units() -> None:
    deletion = (
        "diff --git a/obsolete.py b/obsolete.py\n"
        "deleted file mode 100644\n--- a/obsolete.py\n+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-old"
    )
    files = _parse(deletion, _diff("package-lock.json"))
    plan = DeterministicReviewPlanner().plan(files, base_sha="b", head_sha="h")

    reasons = {unit.primary_files[0]: unit.grouping_reason for unit in plan.review_units}
    assert reasons["obsolete.py"] == "deletion_group"
    assert reasons["package-lock.json"] == "dependency_file"


def test_large_diff_is_split_by_hunk() -> None:
    diff = (
        "diff --git a/large.py b/large.py\n--- a/large.py\n+++ b/large.py\n"
        "@@ -1,3 +1,3 @@\n-a\n-b\n-c\n+x\n+y\n+z\n"
        "@@ -20,3 +20,3 @@\n-d\n-e\n-f\n+u\n+v\n+w"
    )
    planner = DeterministicReviewPlanner(large_min_changed_lines=10)
    plan = planner.plan(_parse(diff), base_sha="b", head_sha="h")

    assert len(plan.review_units) == 2
    assert all(unit.grouping_reason == "large_file_hunk_split" for unit in plan.review_units)
    assert all(len(unit.diff_hunk_ids) == 1 for unit in plan.review_units)


def test_small_unit_skips_plan_and_public_api_unit_enters_plan() -> None:
    planner = DeterministicReviewPlanner()
    small_files = _parse(_diff("value.py", "-VALUE = 1\n+VALUE = 2"))
    public_files = _parse(_diff("api.py", "-def old():\n+def public_api():"))
    small = planner.plan(small_files, base_sha="b", head_sha="h").review_units[0]
    public = planner.plan(public_files, base_sha="b", head_sha="h").review_units[0]

    assert planner.should_skip_plan(small, small_files) is True
    assert planner.should_skip_plan(public, public_files) is False
    assert "public_api" in public.risk_tags


class UnitProvider(LLMProvider):
    def __init__(
        self,
        *,
        fail_paths: set[str] | None = None,
        issue_path: str | None = None,
        delay: float = 0,
    ) -> None:
        self.fail_paths = fail_paths or set()
        self.issue_path = issue_path
        self.delay = delay
        self.decide_calls = 0
        self.active = 0
        self.max_active = 0
        self.started = asyncio.Event()

    async def decide(self, state: dict[str, Any], model: str | None) -> AgentAction:
        self.decide_calls += 1
        return AgentAction(action="review_code", reason="上下文已足够")

    async def review(
        self,
        pr: PullRequestInfo,
        changed_files: list[ChangedFile],
        diff_text: str,
        model: str | None,
    ) -> list[ReviewIssue]:
        path = changed_files[0].file_path
        if path in self.fail_paths:
            raise RuntimeError(f"failed {path}")
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            target = self.issue_path or path
            return [ReviewIssue(
                file_path=target,
                line_no=1,
                severity="low",
                category="correctness",
                title="问题",
                description="可复核的问题描述",
                suggestion="修复它",
                confidence=0.8,
                evidence="精确的代码证据",
                evidence_locations=[{"file_path": target, "line_no": 1}],
                affected_behavior="行为发生变化",
            )]
        finally:
            self.active -= 1

    async def generate_patch(self, state: dict[str, Any], model: str | None) -> list[PatchResult]:
        return []


def _pr() -> PullRequestInfo:
    return PullRequestInfo(
        owner="local",
        repo="sample",
        number=1,
        title="PR",
        html_url="https://github.com/local/sample/pull/1",
        clone_url="https://github.com/local/sample.git",
        base=PullRequestRef(ref="main", sha="b", repo_clone_url="https://github.com/local/sample.git"),
        head=PullRequestRef(ref="feature", sha="h", repo_clone_url="https://github.com/local/sample.git"),
    )


def _state(files: list[ChangedFile]) -> dict[str, Any]:
    return {
        "task_id": "task",
        "pr_info": _pr().model_dump(mode="json"),
        "changed_files": [item.model_dump(mode="json") for item in files],
        "file_index": [{"path": item.file_path, "language": "python", "imports": []} for item in files],
        "symbol_index": [],
        "repo_path": "",
    }


@pytest.mark.asyncio
async def test_one_unit_failure_does_not_stop_other_units_and_order_is_stable() -> None:
    files = _parse(_diff("a.py"), _diff("b.py"), _diff("c.py"))
    units = DeterministicReviewPlanner().plan(files, base_sha="b", head_sha="h").review_units
    provider = UnitProvider(fail_paths={"b.py"}, delay=0.01)
    results = await ReviewUnitExecutor(provider, concurrency=2, timeout_seconds=2).execute(
        units, _state(files)
    )

    assert [item.review_unit_id for item in results] == [unit.id for unit in units]
    assert [item.status for item in results].count(ReviewUnitStatus.completed) == 2
    assert [item.status for item in results].count(ReviewUnitStatus.failed) == 1

    class FixedResults:
        async def execute(self, requested: list[ReviewUnit], state: dict[str, Any]) -> list[ReviewUnitResult]:
            return results

    aggregate = await review_units_node({
        **_state(files),
        "review_plan": DeterministicReviewPlanner().plan(
            files, base_sha="b", head_sha="h"
        ).model_dump(mode="json"),
        "_review_unit_executor": FixedResults(),
        "warnings": [],
    })
    completed = await complete_node(aggregate)
    assert completed["status"] == "completed_with_warnings"


@pytest.mark.asyncio
async def test_all_units_failed_makes_aggregation_fail() -> None:
    files = _parse(_diff("a.py"), _diff("b.py"))
    plan = DeterministicReviewPlanner().plan(files, base_sha="b", head_sha="h")

    class FailedExecutor:
        async def execute(self, units: list[ReviewUnit], state: dict[str, Any]) -> list[ReviewUnitResult]:
            return [ReviewUnitResult(
                review_unit_id=unit.id,
                status=ReviewUnitStatus.failed,
                error="boom",
            ) for unit in units]

    result = await review_units_node({
        **_state(files),
        "review_plan": plan.model_dump(mode="json"),
        "_review_unit_executor": FailedExecutor(),
    })
    assert result["status"] == "failed"
    assert result["error"].startswith("all review units failed")
    assert (await complete_node(result))["status"] == "failed"


@pytest.mark.asyncio
async def test_concurrency_never_exceeds_configured_value() -> None:
    files = _parse(*[_diff(f"f{index}.py") for index in range(6)])
    units = DeterministicReviewPlanner().plan(files, base_sha="b", head_sha="h").review_units
    provider = UnitProvider(delay=0.03)
    await ReviewUnitExecutor(provider, concurrency=2, timeout_seconds=2).execute(units, _state(files))
    assert provider.max_active == 2


@pytest.mark.asyncio
async def test_cancelling_main_dispatch_cancels_running_units() -> None:
    files = _parse(_diff("a.py"), _diff("b.py"))
    units = DeterministicReviewPlanner().plan(files, base_sha="b", head_sha="h").review_units
    provider = UnitProvider(delay=10)
    task = asyncio.create_task(
        ReviewUnitExecutor(provider, concurrency=2, timeout_seconds=30).execute(units, _state(files))
    )
    await provider.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.active == 0


@pytest.mark.asyncio
async def test_related_file_cannot_be_comment_target() -> None:
    files = _parse(_diff("src.py"))
    base_unit = DeterministicReviewPlanner().plan(files, base_sha="b", head_sha="h").review_units[0]
    unit = base_unit.model_copy(update={"related_files": ["related.py"]})
    provider = UnitProvider(issue_path="related.py")
    state = _state(files)
    state["file_index"].append({"path": "related.py", "language": "python", "imports": []})

    result = await ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=2).execute_unit(unit, state)
    assert result.status == ReviewUnitStatus.completed
    assert result.issues == []


@pytest.mark.asyncio
async def test_single_failed_unit_can_be_retried_independently() -> None:
    files = _parse(_diff("retry.py"))
    unit = DeterministicReviewPlanner().plan(
        files, base_sha="b", head_sha="h"
    ).review_units[0]
    provider = UnitProvider(fail_paths={"retry.py"})
    executor = ReviewUnitExecutor(provider, concurrency=1, timeout_seconds=2)

    failed = await executor.execute_unit(unit, _state(files))
    provider.fail_paths.clear()
    retried = await executor.execute_unit(unit, _state(files))

    assert failed.status == ReviewUnitStatus.failed
    assert retried.status == ReviewUnitStatus.completed


@pytest.mark.asyncio
async def test_context_retrieval_plan_is_restricted_to_unit_scope(tmp_path: Path) -> None:
    (tmp_path / "primary.py").write_text("def primary():\n    return 1\n", encoding="utf-8")
    (tmp_path / "outside.py").write_text("def outside():\n    return 2\n", encoding="utf-8")
    unit = ReviewUnit(
        id="unit",
        primary_files=["primary.py"],
        related_files=[],
        diff_hunk_ids=[],
        changed_symbols=["primary"],
        rule_ids=["review.general"],
        risk_tags=[],
        estimated_tokens=512,
        complexity=ReviewUnitComplexity.small,
        fingerprint="fingerprint",
        grouping_reason="single_file",
    )
    scope = DeterministicReviewPlanner().build_scope(unit)
    snippets = await CodeSearchTool().retrieve_context(
        changed_files=[{"file_path": "primary.py"}],
        symbol_index=[],
        file_index=[{"path": "primary.py"}, {"path": "outside.py"}],
        repo_path=str(tmp_path),
        plan={
            "reason": "读取 Unit 内文本",
            "target_files": ["primary.py"],
            "relevance_types": ["text"],
            "search_terms": ["primary"],
        },
        scope=scope,
    )
    assert snippets
    assert {snippet["review_unit_id"] for snippet in snippets} == {unit.id}

    with pytest.raises(ContextRetrievalPlanError, match="outside review unit scope"):
        await CodeSearchTool().retrieve_context(
            changed_files=[{"file_path": "primary.py"}],
            symbol_index=[],
            file_index=[{"path": "primary.py"}, {"path": "outside.py"}],
            repo_path=str(tmp_path),
            plan={
                "reason": "越界读取",
                "target_files": ["outside.py"],
                "relevance_types": ["text"],
                "search_terms": ["outside"],
            },
            scope=scope,
        )


class PreviewGitHub:
    async def fetch_pr(self, pr_url: str) -> PullRequestInfo:
        return _pr()

    async def fetch_diff(self, pr_url: str) -> str:
        return _diff("preview.py")


class RejectingDependency:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"preview must not use dependency method: {name}")


@pytest.mark.asyncio
async def test_preview_does_not_call_llm_or_command_executor(tmp_path: Path) -> None:
    class PreviewGit:
        def clone_and_diff(self, pr: PullRequestInfo) -> tuple[Path, str]:
            repo = tmp_path / "preview-repo"
            repo.mkdir()
            (repo / "preview.py").write_text("new\n", encoding="utf-8")
            return repo, _diff("preview.py")

    service = ReviewService(
        github_tool=PreviewGitHub(),  # type: ignore[arg-type]
        git_tool=PreviewGit(),  # type: ignore[arg-type]
        diff_parser=DiffParser(),
        provider=RejectingDependency(),  # type: ignore[arg-type]
        report_service=ReportService(),
        command_executor=RejectingDependency(),  # type: ignore[arg-type]
    )
    preview = await service.preview(
        ReviewPreviewRequest(pr_url="https://github.com/local/sample/pull/1")
    )
    assert len(preview.review_units) == 1
    assert preview.estimated_model_calls == 1
