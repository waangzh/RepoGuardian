"""受控命令执行器：仅运行项目适配器注册的 CommandSpec。"""

import asyncio
import os
import subprocess
import time
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from app.models.review import CommandId, CommandSpec, TestRunResult
from app.projects.registry import default_project_registry


class CommandPolicyError(ValueError):
    """命令 ID、项目适配器或工作目录不符合策略。"""


class CommandExecutor(Protocol):
    """保留执行接口，后续可替换为 SandboxExecutor。"""

    async def execute(self, repo_path: str | Path, spec: CommandSpec) -> TestRunResult:
        """在指定工作目录运行一个已经注册的命令。"""


class SandboxSpec(BaseModel):
    """真实沙箱实现必须兑现的资源与隔离策略，默认值倾向拒绝和最小权限。"""

    cpu_limit: float = Field(default=1.0, gt=0)
    memory_mb: int = Field(default=512, gt=0)
    pids_limit: int = Field(default=64, gt=0)
    execution_timeout_seconds: int = Field(default=300, gt=0, le=600)
    network_enabled: bool = False
    workspace_mode: Literal["temporary_copy", "read_only_mount"] = "temporary_copy"
    read_only_root: bool = True
    run_as_user: str = "65532:65532"
    max_output_chars: int = Field(default=8_000, gt=0)


class SandboxExecutor(CommandExecutor, Protocol):
    """可替换的沙箱执行边界；实现不得接收模型提供的命令或运行参数。"""

    @property
    def sandbox_spec(self) -> SandboxSpec:
        """返回本执行器实际采用的隔离策略。"""


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
    """仅供开发与可信仓库使用的宿主机执行器，必须显式授权。"""

    def __init__(self, *, allow_unsafe: bool = False, max_output_chars: int = 8_000) -> None:
        self._allow_unsafe = allow_unsafe
        self._max_output_chars = max_output_chars

    async def execute(self, repo_path: str | Path, spec: CommandSpec) -> TestRunResult:
        if not self._allow_unsafe:
            return _failed_result(
                spec,
                125,
                "Unsafe local execution is disabled; configure a sandbox executor or explicitly authorize local execution.",
                0.0,
                self._max_output_chars,
            )
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
            return _result_from_completed(
                spec, completed, time.monotonic() - start, self._max_output_chars
            )
        except FileNotFoundError as exc:
            return _failed_result(spec, 127, str(exc), time.monotonic() - start, self._max_output_chars)
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else "Command timed out"
            return TestRunResult(
                tool=spec.tool,
                command=spec.command_id.value,
                exit_code=124,
                stdout=stdout[-self._max_output_chars :],
                stderr=stderr[-self._max_output_chars :],
                passed=False,
                duration=time.monotonic() - start,
            )
        except OSError as exc:
            return _failed_result(spec, 125, str(exc), time.monotonic() - start, self._max_output_chars)


class RejectedSandboxExecutor:
    """真实沙箱尚未配置时的安全占位：明确拒绝，绝不退回 Local。"""

    def __init__(self, sandbox_spec: SandboxSpec) -> None:
        self._sandbox_spec = sandbox_spec

    @property
    def sandbox_spec(self) -> SandboxSpec:
        return self._sandbox_spec

    async def execute(self, repo_path: str | Path, spec: CommandSpec) -> TestRunResult:
        del repo_path
        return _failed_result(
            spec,
            125,
            "Sandbox execution is required but no sandbox runtime is configured; command was not run.",
            0.0,
            self._sandbox_spec.max_output_chars,
        )


def build_command_executor() -> CommandExecutor:
    """从服务端配置选择执行器；sandbox 模式永不隐式回退到宿主机。"""
    from app.core.config import settings

    if settings.repoguardian_executor == "local":
        return LocalCommandExecutor(
            allow_unsafe=settings.repoguardian_allow_unsafe_local_execution,
            max_output_chars=settings.repoguardian_sandbox_max_output_chars,
        )
    # gVisor remains a safe placeholder: it never falls back to local execution.
    return RejectedSandboxExecutor(
        SandboxSpec(
            cpu_limit=settings.repoguardian_sandbox_cpus,
            memory_mb=settings.repoguardian_sandbox_memory_mb,
            pids_limit=settings.repoguardian_sandbox_pids_limit,
            execution_timeout_seconds=settings.repoguardian_sandbox_timeout_seconds,
            network_enabled=settings.repoguardian_sandbox_network,
            max_output_chars=settings.repoguardian_sandbox_max_output_chars,
        )
    )


def _result_from_completed(
    spec: CommandSpec,
    completed: subprocess.CompletedProcess[str],
    duration: float,
    max_output_chars: int,
) -> TestRunResult:
    return TestRunResult(
        tool=spec.tool,
        command=spec.command_id.value,
        exit_code=completed.returncode,
        stdout=completed.stdout[-max_output_chars:],
        stderr=completed.stderr[-max_output_chars:],
        passed=completed.returncode == 0,
        duration=duration,
    )


def _failed_result(
    spec: CommandSpec,
    exit_code: int,
    stderr: str,
    duration: float,
    max_output_chars: int = 8_000,
) -> TestRunResult:
    return TestRunResult(
        tool=spec.tool,
        command=spec.command_id.value,
        exit_code=exit_code,
        stderr=stderr[-max_output_chars:],
        passed=False,
        duration=duration,
    )
