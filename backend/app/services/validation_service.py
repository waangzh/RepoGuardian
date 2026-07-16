"""三阶段验证的运行、分类与对比逻辑。"""

import re
from pathlib import Path

from app.models.review import (
    FailureKind,
    FailureFingerprint,
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
_DEPENDENCY_ERROR_PREFIXES = (
    "modulenotfounderror: no module named",
    "importerror: no module named",
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
            collected_test_count=_collected_test_count(results),
            failure_fingerprints=extract_failure_fingerprints(results),
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
    lines = [line.strip().lower() for line in f"{failure.stdout}\n{failure.stderr}".splitlines()]
    text = "\n".join(lines)
    if failure.exit_code == 127 or any(
        line.startswith(_DEPENDENCY_ERROR_PREFIXES)
        or ("python" in line and ": no module named" in line)
        for line in lines
    ):
        return FailureKind.dependency_missing
    if failure.command == "python.test.collect":
        return FailureKind.test_collection_error
    if failure.exit_code == 125 or any(marker in text for marker in _INFRASTRUCTURE_MARKERS):
        return FailureKind.infrastructure
    return FailureKind.unknown


def compare_snapshots(previous: ValidationSnapshot, current: ValidationSnapshot) -> ValidationDelta:
    """按失败指纹集合比较，而不是仅以整体通过状态归因。"""
    previous_by_identity = {item.identity: item for item in previous.failure_fingerprints}
    current_by_identity = {item.identity: item for item in current.failure_fingerprints}
    introduced_items = [
        current_by_identity[identity]
        for identity in sorted(current_by_identity.keys() - previous_by_identity.keys())
    ]
    resolved_items = [
        previous_by_identity[identity]
        for identity in sorted(previous_by_identity.keys() - current_by_identity.keys())
    ]

    # 兼容尚无可解析输出的执行器结果，同时优先使用结构化集合。
    introduced = bool(introduced_items) or (
        not previous.failure_fingerprints and not current.failure_fingerprints
        and previous.passed and not current.passed
    )
    resolved = bool(resolved_items) or (
        not previous.failure_fingerprints and not current.failure_fingerprints
        and not previous.passed and current.passed
    )
    if introduced:
        failure_kind: FailureKind | None = FailureKind.code_regression
    elif resolved:
        failure_kind = previous.failure_kind
    else:
        failure_kind = current.failure_kind
    return ValidationDelta(
        from_stage=previous.stage,
        to_stage=current.stage,
        patch_id=current.patch_id,
        previous_passed=previous.passed,
        current_passed=current.passed,
        failure_kind=failure_kind,
        introduced_failure=introduced,
        resolved_failure=resolved,
        introduced_failures=introduced_items,
        resolved_failures=resolved_items,
    )


def blocks_auto_repair(snapshot: ValidationSnapshot) -> bool:
    """环境与基础设施类失败不应被当成可由补丁修复的代码问题。"""
    return snapshot.failure_kind in _NON_REPAIRABLE_FAILURES


def _failure_detail(results: list[TestRunResult]) -> str | None:
    failure = next((item for item in results if not item.passed), None)
    if failure is None:
        return None
    return (failure.stderr or failure.stdout or f"exit code {failure.exit_code}")[:1000]


_RUFF_FAILURE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):(?P<column>\d+):\s*"
    r"(?P<rule>[A-Z]+\d+)\s+(?P<message>.+)$",
    re.MULTILINE,
)
_PYTEST_FAILURE = re.compile(
    r"^FAILED\s+(?P<node>\S+)(?:\s+-\s+(?P<summary>.*))?$", re.MULTILINE
)
_PYTEST_ERROR_TYPE = re.compile(r"\b([A-Za-z_]\w*(?:Error|Exception))\b")
_PYTEST_LOCATION = re.compile(r"(?m)^(?P<file>[^\s:]+\.py):(?P<line>\d+):")
_COLLECTED_TESTS = re.compile(r"\b(\d+)\s+(?:tests?\s+)?collected\b", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")


def extract_failure_fingerprints(results: list[TestRunResult]) -> list[FailureFingerprint]:
    """从 pytest 与 Ruff 的稳定输出提取失败集合；无法解析时保留受控兜底指纹。"""
    fingerprints: dict[str, FailureFingerprint] = {}
    for result in results:
        if result.passed:
            continue
        output = f"{result.stdout}\n{result.stderr}"
        parsed = (
            _pytest_failure_fingerprints(output)
            if result.command.startswith("python.test")
            else _ruff_failure_fingerprints(output)
            if result.command == "python.static.default"
            else []
        )
        if not parsed:
            summary = _normalized_summary(output) or f"exit code {result.exit_code}"
            parsed = [FailureFingerprint(
                tool=result.tool,
                identity=f"unparsed:{result.command}:{summary}",
                message=summary,
                normalized_summary=summary,
            )]
        for item in parsed:
            fingerprints[item.identity] = item
    return [fingerprints[identity] for identity in sorted(fingerprints)]


def _pytest_failure_fingerprints(output: str) -> list[FailureFingerprint]:
    locations = list(_PYTEST_LOCATION.finditer(output))
    fallback_location = locations[-1] if locations else None
    fingerprints: list[FailureFingerprint] = []
    for match in _PYTEST_FAILURE.finditer(output):
        node_id = match.group("node")
        summary = _normalized_summary(match.group("summary") or "pytest failure")
        error_match = _PYTEST_ERROR_TYPE.search(match.group("summary") or "")
        if error_match is None:
            error_match = _PYTEST_ERROR_TYPE.search(output)
        error_type = error_match.group(1) if error_match else None
        location = fallback_location
        file_path = location.group("file") if location else _node_file(node_id)
        line_no = int(location.group("line")) if location else None
        identity = ":".join([
            "pytest",
            node_id,
            error_type or "unknown",
            file_path or "unknown",
            str(line_no or 0),
            summary,
        ])
        fingerprints.append(FailureFingerprint(
            tool="pytest",
            identity=identity,
            test_node_id=node_id,
            error_type=error_type,
            file_path=file_path,
            line_no=line_no,
            message=match.group("summary") or None,
            normalized_summary=summary,
        ))
    return fingerprints


def _ruff_failure_fingerprints(output: str) -> list[FailureFingerprint]:
    fingerprints: list[FailureFingerprint] = []
    for match in _RUFF_FAILURE.finditer(output):
        file_path = match.group("file")
        line_no = int(match.group("line"))
        column = int(match.group("column"))
        rule_code = match.group("rule")
        message = match.group("message")
        summary = _normalized_summary(message)
        identity = f"ruff:{rule_code}:{file_path}:{line_no}:{column}:{summary}"
        fingerprints.append(FailureFingerprint(
            tool="ruff",
            identity=identity,
            file_path=file_path,
            line_no=line_no,
            column=column,
            rule_code=rule_code,
            message=message,
            normalized_summary=summary,
        ))
    return fingerprints


def _collected_test_count(results: list[TestRunResult]) -> int | None:
    for result in results:
        if not result.command.startswith("python.test"):
            continue
        match = _COLLECTED_TESTS.search(f"{result.stdout}\n{result.stderr}")
        if match:
            return int(match.group(1))
    return None


def _node_file(node_id: str) -> str | None:
    return node_id.split("::", 1)[0] if ".py" in node_id else None


def _normalized_summary(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip().lower()[:500]
