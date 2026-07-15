from pathlib import Path

import pytest

from app.models.review import CommandId, PatchResult
from app.tools.command_runner import CommandPolicyError, resolve_command_spec
from app.tools.patch_tool import PatchTool
from app.tools.test_runner import TestRunnerTool


def test_resolve_command_spec_rejects_unknown_command_id() -> None:
    with pytest.raises(CommandPolicyError):
        resolve_command_spec("rm -rf .")


def test_resolve_command_spec_allows_registered_pytest_command() -> None:
    spec = resolve_command_spec(CommandId.python_test_full)

    assert spec.command_id == CommandId.python_test_full
    assert "-m" in spec.argv


@pytest.mark.asyncio
async def test_test_runner_returns_structured_result(tmp_path: Path) -> None:
    result = await TestRunnerTool().execute(
        repo_path=str(tmp_path), command_id=CommandId.python_test_full
    )

    assert result["test_results"][0]["command"] == "python.test.full"
    assert isinstance(result["test_results"][0]["exit_code"], int)


@pytest.mark.asyncio
async def test_patch_tool_rejects_empty_diff(tmp_path: Path) -> None:
    patch = PatchResult(diff_content="")
    result = await PatchTool().apply(tmp_path, patch)

    assert result.status == "apply_failed"
