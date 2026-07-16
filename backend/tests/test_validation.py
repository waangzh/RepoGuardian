from pathlib import Path

import pytest

from app.graph.nodes.baseline import baseline_node
from app.graph.nodes.repair_policy import repair_policy_node
from app.models.review import (
    CommandSpec,
    ExecutionBudget,
    FailureFingerprint,
    FailureKind,
    TestRunResult as RunResult,
    ValidationSnapshot,
    ValidationStage,
)
from app.projects.python import PythonProjectAdapter
from app.services.validation_service import (
    blocks_auto_repair,
    classify_failure,
    compare_snapshots,
    extract_failure_fingerprints,
)


def _result(
    *,
    passed: bool,
    command: str = "python.test.full",
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
) -> RunResult:
    return RunResult(
        tool="test_runner",
        command=command,
        exit_code=exit_code if not passed else 0,
        stdout=stdout,
        stderr=stderr,
        passed=passed,
    )


def _snapshot(stage: ValidationStage, passed: bool, failure_kind: FailureKind | None = None) -> ValidationSnapshot:
    return ValidationSnapshot(
        stage=stage,
        sha=f"{stage.value}-sha",
        command_results=[_result(passed=passed)],
        passed=passed,
        failure_kind=failure_kind,
    )


@pytest.fixture
def base_pass_head_fail() -> tuple[ValidationSnapshot, ValidationSnapshot]:
    return _snapshot(ValidationStage.base, True), _snapshot(ValidationStage.head, False, FailureKind.unknown)


@pytest.fixture
def base_fail_head_fail() -> tuple[ValidationSnapshot, ValidationSnapshot]:
    return (
        _snapshot(ValidationStage.base, False, FailureKind.unknown),
        _snapshot(ValidationStage.head, False, FailureKind.unknown),
    )


@pytest.fixture
def dependency_missing_result() -> RunResult:
    return _result(passed=False, stderr="ModuleNotFoundError: No module named 'pytest'", exit_code=1)


@pytest.fixture
def collection_error_result() -> RunResult:
    return _result(passed=False, command="python.test.collect", stderr="ERROR collecting tests", exit_code=2)


@pytest.fixture
def patch_regression() -> tuple[ValidationSnapshot, ValidationSnapshot]:
    return _snapshot(ValidationStage.head, True), _snapshot(ValidationStage.patched, False, FailureKind.unknown)


@pytest.fixture
def patch_fix() -> tuple[ValidationSnapshot, ValidationSnapshot]:
    return _snapshot(ValidationStage.head, False, FailureKind.unknown), _snapshot(ValidationStage.patched, True)


def test_base_pass_head_fail_is_code_regression(base_pass_head_fail: tuple[ValidationSnapshot, ValidationSnapshot]) -> None:
    delta = compare_snapshots(*base_pass_head_fail)

    assert delta.introduced_failure is True
    assert delta.failure_kind == FailureKind.code_regression


def test_base_fail_head_fail_is_not_code_regression(base_fail_head_fail: tuple[ValidationSnapshot, ValidationSnapshot]) -> None:
    delta = compare_snapshots(*base_fail_head_fail)

    assert delta.introduced_failure is False
    assert delta.failure_kind == FailureKind.unknown


def test_missing_dependency_is_environment_failure(dependency_missing_result: RunResult) -> None:
    snapshot = _snapshot(ValidationStage.head, False, classify_failure([dependency_missing_result]))

    assert snapshot.failure_kind == FailureKind.dependency_missing
    assert blocks_auto_repair(snapshot) is True


def test_business_error_text_is_not_misclassified_as_missing_dependency() -> None:
    result = _result(
        passed=False,
        stderr='AssertionError: expected "no such file or directory" in API response',
        exit_code=1,
    )
    snapshot = _snapshot(ValidationStage.head, False, classify_failure([result]))

    assert snapshot.failure_kind == FailureKind.unknown
    assert blocks_auto_repair(snapshot) is False


def test_test_collection_failure_is_classified_and_blocks_repair(collection_error_result: RunResult) -> None:
    snapshot = _snapshot(ValidationStage.head, False, classify_failure([collection_error_result]))

    assert snapshot.failure_kind == FailureKind.test_collection_error
    assert blocks_auto_repair(snapshot) is True


def test_patch_introduced_regression_is_detected(patch_regression: tuple[ValidationSnapshot, ValidationSnapshot]) -> None:
    delta = compare_snapshots(*patch_regression)

    assert delta.from_stage == ValidationStage.head
    assert delta.to_stage == ValidationStage.patched
    assert delta.failure_kind == FailureKind.code_regression


def test_patch_fix_resolves_head_failure(patch_fix: tuple[ValidationSnapshot, ValidationSnapshot]) -> None:
    delta = compare_snapshots(*patch_fix)

    assert delta.resolved_failure is True
    assert delta.failure_kind == FailureKind.unknown


class CheckoutFixtureGitTool:
    def __init__(self) -> None:
        self.checkouts: list[str] = []

    def checkout_sha(self, repo_path: str, sha: str) -> None:
        self.checkouts.append(sha)


class PassingExecutor:
    async def execute(self, repo_path: str, spec: CommandSpec) -> RunResult:
        return _result(passed=True, command=spec.command_id.value)


@pytest.mark.asyncio
async def test_baseline_validation_checkouts_base_then_head(tmp_path: Path) -> None:
    (tmp_path / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    profile = PythonProjectAdapter().detect(tmp_path)
    assert profile is not None
    git_tool = CheckoutFixtureGitTool()

    result = await baseline_node({
        "repo_path": str(tmp_path),
        "base_sha": "base-sha",
        "head_sha": "head-sha",
        "project_profile": profile.model_dump(mode="json"),
        "_git_tool": git_tool,
        "_command_executor": PassingExecutor(),
    })

    assert git_tool.checkouts == ["base-sha", "head-sha"]
    assert [item["stage"] for item in result["validation_snapshots"]] == ["base", "head"]
    assert result["validation_deltas"][0]["failure_kind"] is None


@pytest.mark.asyncio
async def test_dependency_failure_disables_repair_policy() -> None:
    result = await repair_policy_node({
        "validation_blocked": True,
        "execution_budget": ExecutionBudget().model_dump(),
        "review_issues": [{"auto_fixable": True}],
    })

    assert result["repair_enabled"] is False


def _fingerprint(identity: str) -> FailureFingerprint:
    return FailureFingerprint(tool="pytest", identity=identity, normalized_summary=identity)


def test_head_new_failure_is_detected_when_base_already_failed() -> None:
    base = _snapshot(ValidationStage.base, False, FailureKind.unknown)
    base.failure_fingerprints = [_fingerprint("pytest:tests/test_old.py::test_old")]
    head = _snapshot(ValidationStage.head, False, FailureKind.unknown)
    head.failure_fingerprints = base.failure_fingerprints + [
        _fingerprint("pytest:tests/test_new.py::test_new")
    ]

    delta = compare_snapshots(base, head)

    assert delta.introduced_failure is True
    assert [item.identity for item in delta.introduced_failures] == [
        "pytest:tests/test_new.py::test_new"
    ]


def test_resolved_failure_and_new_failure_are_both_reported() -> None:
    head = _snapshot(ValidationStage.head, False, FailureKind.unknown)
    head.failure_fingerprints = [_fingerprint("pytest:tests/test_target.py::test_target")]
    patched = _snapshot(ValidationStage.patched, False, FailureKind.unknown)
    patched.failure_fingerprints = [_fingerprint("pytest:tests/test_regression.py::test_regression")]

    delta = compare_snapshots(head, patched)

    assert delta.introduced_failure is True
    assert delta.resolved_failure is True
    assert delta.failure_kind == FailureKind.code_regression


def test_failure_output_order_does_not_create_a_regression() -> None:
    base = _snapshot(ValidationStage.base, False, FailureKind.unknown)
    base.failure_fingerprints = [_fingerprint("a"), _fingerprint("b")]
    head = _snapshot(ValidationStage.head, False, FailureKind.unknown)
    head.failure_fingerprints = [_fingerprint("b"), _fingerprint("a")]

    delta = compare_snapshots(base, head)

    assert delta.introduced_failure is False
    assert delta.resolved_failure is False


def test_pytest_and_ruff_failures_are_parsed_into_structured_fingerprints() -> None:
    pytest_result = _result(
        passed=False,
        stdout="2 tests collected\nFAILED tests/test_math.py::test_add - AssertionError: expected 2\n",
        stderr="tests/test_math.py:12: AssertionError\n",
        exit_code=1,
    )
    ruff_result = _result(
        passed=False,
        command="python.static.default",
        stdout="app.py:4:7: F401 `os` imported but unused\n",
        exit_code=1,
    )

    fingerprints = extract_failure_fingerprints([pytest_result, ruff_result])

    pytest_fingerprint = next(item for item in fingerprints if item.tool == "pytest")
    ruff_fingerprint = next(item for item in fingerprints if item.tool == "ruff")
    assert pytest_fingerprint.test_node_id == "tests/test_math.py::test_add"
    assert pytest_fingerprint.error_type == "AssertionError"
    assert pytest_fingerprint.line_no == 12
    assert ruff_fingerprint.rule_code == "F401"
    assert ruff_fingerprint.file_path == "app.py"
    assert ruff_fingerprint.column == 7
