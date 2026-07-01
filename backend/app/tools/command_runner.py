import asyncio
import subprocess
import sys
import time
from pathlib import Path

from app.models.review import TestRunResult


class CommandPolicyError(ValueError):
    pass


_ALLOWED_COMMANDS: dict[str, tuple[str, ...]] = {
    "ruff check .": ("ruff", "check", "."),
    "python -m pytest -q": (sys.executable, "-m", "pytest", "-q"),
}


def resolve_allowed_command(command: str | None, default: str) -> tuple[str, list[str]]:
    selected = command or default
    if selected not in _ALLOWED_COMMANDS:
        raise CommandPolicyError(f"Command is not allowed: {selected}")
    return selected, list(_ALLOWED_COMMANDS[selected])


def ensure_repo_path(repo_path: str | Path) -> Path:
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
            stdout=completed.stdout[-8000:],
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
