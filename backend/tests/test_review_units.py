import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.agents.providers import LLMProvider
from app.graph.nodes.report import complete_node, report_node
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
    UnitPlanStatus,
    UnitReviewPlan,
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
    assert planner.planning_model_calls(small, small_files) == 0
    assert planner.planning_model_calls(public, public_files) == 1
    assert planner.estimated_model_calls(small, small_files) == 2
    assert planner.estimated_model_calls(public, public_files) == 4
    assert planner.max_model_calls(small) == 3
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
        self.decision_states: list[dict[str, Any]] = []
        self.review_diffs: list[str] = []
        self.active = 0
        self.max_active = 0
        self.started = asyncio.Event()

    async def plan_review_unit(
        self, state: dict[str, Any], model: str | None
    ) -> UnitReviewPlan:
        return UnitReviewPlan.model_validate({
            "change_summary": "检查 Unit 变更",
            "review_objectives": ["验证行为"],
            "risk_hypotheses": [],
            "coverage_targets": ["变更路径"],
            "initial_action": {"action": "report_issue", "reason": "进入审查"},
        })

    async def decide(self, state: dict[str, Any], model: str | None) -> AgentAction:
        self.decide_calls += 1
        self.decision_states.append(state)
        return AgentAction(action="review_code", reason="上下文已足够")

    async def review(
        self,
        pr: PullRequestInfo,
        changed_files: list[ChangedFile],
        diff_text: str,
        model: str | None,
    ) -> list[ReviewIssue]:
        self.review_diffs.append(diff_text)
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
            first_hunk = changed_files[0].hunks[0]
            evidence_code = (
                first_hunk.added_lines[0].content
                if first_hunk.added_lines else first_hunk.removed_lines[0].content
            )
            return [ReviewIssue(
                review_unit_id="unassigned",
                severity="low",
                category="correctness",
                title="问题",
                failure_scenario="可复核的问题描述",
                recommendation="修复它",
                confidence=0.8,
                primary_evidence={"file_path": target, "existing_code": evidence_code},
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
async def test_report_uses_final_status_and_original_created_at() -> None:
    result = await report_node({
        **_state([]),
        "status": "verifying_issues",
        "created_at": "2026-08-07T09:59:38+00:00",
        "updated_at": "2026-08-07T09:59:40+00:00",
        "warnings": [],
    })

    assert result["status"] == "completed"
    assert result["phase"] == "completed"
    assert "- 状态：completed" in result["report_markdown"]
    assert "- 创建时间：2026-08-07T09:59:38+00:00" in result["report_markdown"]


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
    assert result.issues[0].primary_evidence.file_path == "related.py"


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
async def test_unit_plan_failure_degrades_to_normal_decision() -> None:
    files = _parse(_diff("api.py", "-def old():\n+def public_api():"))
    unit = DeterministicReviewPlanner().plan(
        files, base_sha="b", head_sha="h"
    ).review_units[0]

    class FailingPlanProvider(UnitProvider):
        async def plan_review_unit(self, state: dict[str, Any], model: str | None) -> UnitReviewPlan:
            raise RuntimeError("invalid plan")

    provider = FailingPlanProvider()
    result = await ReviewUnitExecutor(
        provider, concurrency=1, timeout_seconds=2
    ).execute_unit(unit, _state(files))

    assert result.status == ReviewUnitStatus.completed
    assert result.plan is None
    assert result.plan_status == UnitPlanStatus.failed
    assert result.plan_skip_reason == "planning_failed"
    assert "invalid plan" in (result.plan_error or "")
    assert provider.decide_calls == 1


@pytest.mark.asyncio
async def test_successful_unit_plan_is_injected_into_decide_and_review() -> None:
    files = _parse(_diff("api.py", "-def old():\n+def public_api():"))
    unit = DeterministicReviewPlanner().plan(
        files, base_sha="b", head_sha="h"
    ).review_units[0]
    provider = UnitProvider()

    result = await ReviewUnitExecutor(
        provider, concurrency=1, timeout_seconds=2
    ).execute_unit(unit, _state(files))

    assert result.plan_status == UnitPlanStatus.planned
    assert result.plan
    assert provider.decision_states[0]["unit_plan"]["change_summary"]
    assert provider.decision_states[0]["unit_diff"]
    assert "risk hypotheses are unconfirmed guidance" in provider.review_diffs[0]


@pytest.mark.asyncio
async def test_out_of_scope_unit_plan_is_rejected_and_degraded() -> None:
    files = _parse(_diff("api.py", "-def old():\n+def public_api():"))
    unit = DeterministicReviewPlanner().plan(
        files, base_sha="b", head_sha="h"
    ).review_units[0]

    class OutOfScopePlanProvider(UnitProvider):
        async def plan_review_unit(self, state: dict[str, Any], model: str | None) -> UnitReviewPlan:
            return UnitReviewPlan.model_validate({
                "change_summary": "尝试越界读取",
                "review_objectives": ["验证行为"],
                "risk_hypotheses": [{
                    "id": "risk-outside",
                    "category": "security",
                    "priority": "high",
                    "description": "需要读取 Unit 外文件",
                    "affected_files": ["outside.py"],
                    "affected_symbols": [],
                    "evidence_needed": ["读取越界文件"],
                    "retrieval_suggestions": [],
                    "completion_criteria": "确认越界内容",
                }],
                "coverage_targets": ["越界路径"],
                "initial_action": {"action": "report_issue", "reason": "进入审查"},
            })

    provider = OutOfScopePlanProvider()
    result = await ReviewUnitExecutor(
        provider, concurrency=1, timeout_seconds=2
    ).execute_unit(unit, _state(files))

    assert result.status == ReviewUnitStatus.completed
    assert result.plan_status == UnitPlanStatus.failed
    assert "outside review scope" in (result.plan_error or "")
    assert provider.decide_calls == 1


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


def test_repository_graph_builds_layered_scope_with_provenance() -> None:
    files = _parse(_diff("app/services/user.py"))
    file_index = [
        {"path": "app/services/user.py", "language": "python", "imports": ["app.models.user"]},
        {"path": "app/models/user.py", "language": "python", "imports": ["app.schemas.user"]},
        {"path": "app/schemas/user.py", "language": "python", "imports": []},
        {"path": "tests/test_user.py", "language": "python", "imports": ["app.services.user"]},
        {"path": "pyproject.toml", "language": "unknown", "imports": []},
    ]
    from app.tools.repository_graph import build_repository_graph

    graph = build_repository_graph(file_index, [])
    unit = DeterministicReviewPlanner().plan(
        files,
        base_sha="b",
        head_sha="h",
        file_index=file_index,
        repository_graph=graph,
    ).review_units[0]
    provenance = {item.file: item for item in unit.context_provenance}

    assert provenance["app/models/user.py"].source == "import"
    assert provenance["app/models/user.py"].distance == 1
    assert provenance["tests/test_user.py"].source == "test"
    assert provenance["tests/test_user.py"].distance == 1
    assert provenance["app/schemas/user.py"].source == "dependency"
    assert provenance["app/schemas/user.py"].distance == 2
    assert provenance["pyproject.toml"].source == "config"
    assert all(item.unit_id == unit.id for item in provenance.values())


@pytest.mark.asyncio
async def test_code_search_attaches_scope_provenance(tmp_path: Path) -> None:
    (tmp_path / "primary.py").write_text("def primary():\n    return 1\n", encoding="utf-8")
    (tmp_path / "related.py").write_text("VALUE = 1\n", encoding="utf-8")
    unit = ReviewUnit(
        id="unit",
        primary_files=["primary.py"],
        related_files=["related.py"],
        context_provenance=[{
            "file": "related.py", "source": "caller", "distance": 1,
            "confidence": 0.94, "why_retrieved": "related.py calls primary",
            "unit_id": "unit",
        }],
        diff_hunk_ids=[], changed_symbols=[], rule_ids=[], risk_tags=[],
        estimated_tokens=512, complexity=ReviewUnitComplexity.small,
        fingerprint="fingerprint", grouping_reason="single_file",
    )
    scope = DeterministicReviewPlanner().build_scope(unit)
    snippets = await CodeSearchTool().retrieve_context(
        changed_files=[{"file_path": "primary.py"}],
        symbol_index=[],
        file_index=[{"path": "primary.py"}, {"path": "related.py"}],
        repo_path=str(tmp_path),
        plan={
            "reason": "read approved related file", "target_files": ["related.py"],
            "relevance_types": ["text"], "search_terms": ["VALUE"],
        },
        scope=scope,
    )
    assert snippets[0]["source"] == "caller"
    assert snippets[0]["distance"] == 1
    assert snippets[0]["confidence"] == 0.94
    assert snippets[0]["why_retrieved"] == "related.py calls primary"


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
        def clone_and_diff(self, pr: PullRequestInfo, **_kwargs: object) -> tuple[Path, str]:
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
    assert preview.planning_model_calls == 0
    assert preview.estimated_model_calls == 2
    assert preview.max_model_calls == 3
