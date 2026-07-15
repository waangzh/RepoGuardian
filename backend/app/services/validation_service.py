"""三阶段验证的运行、分类与对比逻辑。"""

from pathlib import Path

from app.models.review import (
    FailureKind,
    ProjectProfile,
    TestRunResult,
    ValidationDelta,
    ValidationSnapshot,
    ValidationStage,
)
from app.projects.adapter import ProjectAdapter
from app.tools.command_runner import CommandExecutor


_NON_REPAIRABLE_FAILURES = frozenset({
    FailureKind.dependency_missing,
    FailureKind.test_collection_error,
    FailureKind.timeout,
    FailureKind.infrastructure,
})
_DEPENDENCY_MARKERS = (
    "modulenotfounderror",
    "no module named",
    "package not found",
    "command not found",
    "no such file or directory",
)
_INFRASTRUCTURE_MARKERS = (
    "permission denied",
    "resource temporarily unavailable",
    "internal error",
)


class ValidationService:
    """以适配器的固定命令集合执行一个阶段的验证。"""

    def __init__(self, adapter: ProjectAdapter, executor: CommandExecutor) -> None:
        self._adapter = adapter
        self._executor = executor

    async def run_stage(
        self,
        repo_path: str | Path,
        profile: ProjectProfile,
        stage: ValidationStage,
        sha: str,
    ) -> ValidationSnapshot:
        results: list[TestRunResult] = []
        for command_id in profile.validation_command_ids:
            try:
                spec = self._adapter.command_spec(command_id)
                result = await self._executor.execute(repo_path, spec)
            except Exception as exc:
                result = TestRunResult(
                    tool="validation",
                    command=command_id.value,
                    exit_code=125,
                    stderr=f"validation executor failed: {type(exc).__name__}: {exc}",
                    passed=False,
                )
            results.append(result)
            if not result.passed:
                break

        failure_kind = classify_failure(results)
        return ValidationSnapshot(
            stage=stage,
            sha=sha,
            command_results=results,
            passed=failure_kind is None,
            failure_kind=failure_kind,
            failure_detail=_failure_detail(results) if failure_kind else None,
        )


def classify_failure(results: list[TestRunResult]) -> FailureKind | None:
    """从原始进程结果中区分依赖、收集、超时和基础设施失败。"""
    failure = next((item for item in results if not item.passed), None)
    if failure is None:
        return None
    if failure.exit_code == 124:
        return FailureKind.timeout
    text = f"{failure.stdout}\n{failure.stderr}".lower()
    if failure.exit_code == 127 or any(marker in text for marker in _DEPENDENCY_MARKERS):
        return FailureKind.dependency_missing
    if failure.command == "python.test.collect":
        return FailureKind.test_collection_error
    if failure.exit_code == 125 or any(marker in text for marker in _INFRASTRUCTURE_MARKERS):
        return FailureKind.infrastructure
    return FailureKind.unknown


def compare_snapshots(previous: ValidationSnapshot, current: ValidationSnapshot) -> ValidationDelta:
    """仅将“通过变失败”标记为代码回归，避免把既有失败误归因给 PR。"""
    introduced = previous.passed and not current.passed
    resolved = not previous.passed and current.passed
    if introduced:
        failure_kind: FailureKind | None = FailureKind.code_regression
    elif resolved:
        failure_kind = previous.failure_kind
    else:
        failure_kind = current.failure_kind
    return ValidationDelta(
        from_stage=previous.stage,
        to_stage=current.stage,
        previous_passed=previous.passed,
        current_passed=current.passed,
        failure_kind=failure_kind,
        introduced_failure=introduced,
        resolved_failure=resolved,
    )


def blocks_auto_repair(snapshot: ValidationSnapshot) -> bool:
    """环境与基础设施类失败不应被当成可由补丁修复的代码问题。"""
    return snapshot.failure_kind in _NON_REPAIRABLE_FAILURES


def _failure_detail(results: list[TestRunResult]) -> str | None:
    failure = next((item for item in results if not item.passed), None)
    if failure is None:
        return None
    return (failure.stderr or failure.stdout or f"exit code {failure.exit_code}")[:1000]
