from pathlib import Path

import pytest

from app.models.review import PatchResult
from app.tools.command_runner import CommandPolicyError, resolve_allowed_command
from app.tools.patch_tool import PatchTool
from app.tools.test_runner import TestRunnerTool


def test_resolve_allowed_command_rejects_unknown_command() -> None:
    with pytest.raises(CommandPolicyError):
        resolve_allowed_command("rm -rf .", "ruff check .")


def test_resolve_allowed_command_allows_pytest() -> None:
    name, argv = resolve_allowed_command("python -m pytest -q", "ruff check .")

    assert name == "python -m pytest -q"
    assert "-m" in argv


@pytest.mark.asyncio
async def test_test_runner_returns_structured_result(tmp_path: Path) -> None:
    result = await TestRunnerTool().execute(repo_path=str(tmp_path), command="python -m pytest -q")

    assert result["test_results"][0]["command"] == "python -m pytest -q"
    assert isinstance(result["test_results"][0]["exit_code"], int)


@pytest.mark.asyncio
async def test_patch_tool_rejects_empty_diff(tmp_path: Path) -> None:
    patch = PatchResult(diff_content="")
    result = await PatchTool().apply(tmp_path, patch)

    assert result.status == "apply_failed"
