"""受控命令执行器：仅运行项目适配器注册的 CommandSpec。"""

import asyncio
import os
import subprocess
import time
from pathlib import Path
from typing import Protocol

from app.models.review import CommandId, CommandSpec, TestRunResult
from app.projects.registry import default_project_registry


class CommandPolicyError(ValueError):
    """命令 ID、项目适配器或工作目录不符合策略。"""


class CommandExecutor(Protocol):
    """保留执行接口，后续可替换为 SandboxExecutor。"""

    async def execute(self, repo_path: str | Path, spec: CommandSpec) -> TestRunResult:
        """在指定工作目录运行一个已经注册的命令。"""


def ensure_repo_path(repo_path: str | Path) -> Path:
    """验证仓库路径存在且是目录。"""
    path = Path(repo_path).resolve()
    if not path.exists() or not path.is_dir():
        raise CommandPolicyError(f"Repository path does not exist: {path}")
    return path


def resolve_command_spec(command_id: CommandId | str, adapter_id: str = "python") -> CommandSpec:
    """从服务端注册表解析命令，拒绝自由格式命令字符串。"""
    try:
        normalized_id = CommandId(command_id)
    except ValueError as exc:
        raise CommandPolicyError(f"Command is not allowed: {command_id}") from exc
    adapter = default_project_registry.get(adapter_id)
    if adapter is None:
        raise CommandPolicyError(f"Project adapter is not registered: {adapter_id}")
    try:
        return adapter.command_spec(normalized_id)
    except ValueError as exc:
        raise CommandPolicyError(str(exc)) from exc


def build_safe_execution_environment() -> dict[str, str]:
    """不继承宿主环境，避免令牌、模型密钥和代理设置泄漏到仓库测试。"""
    environment = {
        "NO_COLOR": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUTF8": "1",
    }
    if os.name == "nt" and (system_root := os.environ.get("SystemRoot")):
        # Windows 进程创建要求此系统路径；其余宿主环境变量均不会继承。
        environment["SystemRoot"] = system_root
    return environment


class LocalCommandExecutor:
    """本地受控执行器；仅作为未来 SandboxExecutor 的可替换默认实现。"""

    async def execute(self, repo_path: str | Path, spec: CommandSpec) -> TestRunResult:
        cwd = ensure_repo_path(repo_path)
        start = time.monotonic()
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                list(spec.argv),
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=spec.timeout_seconds,
                env=build_safe_execution_environment(),
            )
            return _result_from_completed(spec, completed, time.monotonic() - start)
        except FileNotFoundError as exc:
            return _failed_result(spec, 127, str(exc), time.monotonic() - start)
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else "Command timed out"
            return TestRunResult(
                tool=spec.tool,
                command=spec.command_id.value,
                exit_code=124,
                stdout=stdout[-8000:],
                stderr=stderr[-8000:],
                passed=False,
                duration=time.monotonic() - start,
            )
        except OSError as exc:
            return _failed_result(spec, 125, str(exc), time.monotonic() - start)


def _result_from_completed(
    spec: CommandSpec,
    completed: subprocess.CompletedProcess[str],
    duration: float,
) -> TestRunResult:
    return TestRunResult(
        tool=spec.tool,
        command=spec.command_id.value,
        exit_code=completed.returncode,
        stdout=completed.stdout[-8000:],
        stderr=completed.stderr[-8000:],
        passed=completed.returncode == 0,
        duration=duration,
    )


def _failed_result(spec: CommandSpec, exit_code: int, stderr: str, duration: float) -> TestRunResult:
    return TestRunResult(
        tool=spec.tool,
        command=spec.command_id.value,
        exit_code=exit_code,
        stderr=stderr[-8000:],
        passed=False,
        duration=duration,
    )
