"""命令运行器 —— 白名单 subprocess 执行引擎。

安全策略：
    - 仅允许 _ALLOWED_COMMANDS 中注册的命令（ruff check . / pytest -q）
    - 所有命令在指定 repo_path 下运行
    - 通过 asyncio.to_thread 包装 subprocess，不阻塞事件循环
"""

import asyncio
import subprocess
import sys
import time
from pathlib import Path

from app.models.review import TestRunResult


class CommandPolicyError(ValueError):
    """命令不在白名单时抛出。"""
    pass


# 白名单：只允许这两条命令，防止任意命令注入
_ALLOWED_COMMANDS: dict[str, tuple[str, ...]] = {
    "ruff check .": ("ruff", "check", "."),
    "python -m pytest -q": (sys.executable, "-m", "pytest", "-q"),
}


def resolve_allowed_command(command: str | None, default: str) -> tuple[str, list[str]]:
    """验证命令在白名单中，返回 (命令名, 参数列表)。"""
    selected = command or default
    if selected not in _ALLOWED_COMMANDS:
        raise CommandPolicyError(f"Command is not allowed: {selected}")
    return selected, list(_ALLOWED_COMMANDS[selected])


def ensure_repo_path(repo_path: str | Path) -> Path:
    """验证仓库路径存在且为目录。"""
    path = Path(repo_path).resolve()
    if not path.exists() or not path.is_dir():
        raise CommandPolicyError(f"Repository path does not exist: {path}")
    return path


async def run_command(
    repo_path: str | Path,
    command: str | None,
    default: str,
    tool: str,
    timeout_seconds: int = 60,
) -> TestRunResult:
    """在仓库目录中异步执行白名单命令，捕获 stdout/stderr 和耗时。

    异常处理：
        - FileNotFoundError   → exit_code=127
        - TimeoutExpired       → exit_code=124
        - 正常退出             → 返回实际 exit_code
    """
    command_name, argv = resolve_allowed_command(command, default)
    cwd = ensure_repo_path(repo_path)
    start = time.monotonic()
    try:
        completed = await asyncio.to_thread(
            subprocess.run,
            argv,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
        duration = time.monotonic() - start
        return TestRunResult(
            tool=tool,
            command=command_name,
            exit_code=completed.returncode,
            stdout=completed.stdout[-8000:],     # 截断超长输出
            stderr=completed.stderr[-8000:],
            passed=completed.returncode == 0,
            duration=duration,
        )
    except FileNotFoundError as exc:
        duration = time.monotonic() - start
        return TestRunResult(
            tool=tool,
            command=command_name,
            exit_code=127,
            stdout="",
            stderr=str(exc),
            passed=False,
            duration=duration,
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - start
        return TestRunResult(
            tool=tool,
            command=command_name,
            exit_code=124,
            stdout=(exc.stdout or "")[-8000:] if isinstance(exc.stdout, str) else "",
            stderr=(exc.stderr or "Command timed out")[-8000:] if isinstance(exc.stderr, str) else "Command timed out",
            passed=False,
            duration=duration,
        )
