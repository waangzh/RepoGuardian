from pathlib import Path

import pytest

from app.models.review import CommandId
from app.tools.command_runner import CommandPolicyError, LocalCommandExecutor, resolve_command_spec


def test_resolve_command_spec_rejects_unknown_command_id() -> None:
    with pytest.raises(CommandPolicyError):
        resolve_command_spec("rm -rf .")


def test_resolve_command_spec_allows_registered_pytest_command() -> None:
    spec = resolve_command_spec(CommandId.python_test_full)

    assert spec.command_id == CommandId.python_test_full
    assert "-m" in spec.argv


@pytest.mark.asyncio
async def test_local_execution_is_rejected_without_explicit_unsafe_authorization(tmp_path: Path) -> None:
    spec = resolve_command_spec(CommandId.python_test_full)

    result = await LocalCommandExecutor().execute(tmp_path, spec)

    assert result.passed is False
    assert result.exit_code == 125
    assert "Unsafe local execution is disabled" in result.stderr
