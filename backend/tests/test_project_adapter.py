from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models.review import AgentAction, CommandId
from app.projects.python import PythonProjectAdapter
from app.tools.command_runner import build_safe_execution_environment


def test_python_adapter_recognizes_supported_project_markers(tmp_path: Path) -> None:
    markers = (
        "pyproject.toml",
        "requirements.txt",
        "poetry.lock",
        "uv.lock",
        "Pipfile.lock",
        "pytest.ini",
        "tox.ini",
        "setup.cfg",
    )
    for marker in markers:
        (tmp_path / marker).write_text("# fixture\n", encoding="utf-8")

    profile = PythonProjectAdapter().detect(tmp_path)

    assert profile is not None
    assert profile.adapter_id == "python"
    assert profile.detected_files == list(markers)
    assert profile.validation_command_ids == [
        CommandId.python_test_collect,
        CommandId.python_test_full,
    ]


def test_python_adapter_registers_only_the_supported_command_ids(tmp_path: Path) -> None:
    adapter = PythonProjectAdapter()
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")

    assert adapter.detect(tmp_path) is not None
    assert adapter.command_spec(CommandId.python_static_default).argv[-2:] == ("check", ".")
    assert adapter.command_spec(CommandId.python_test_collect).argv[-2:] == ("--collect-only", "-q")
    assert adapter.command_spec(CommandId.python_test_targeted).command_id == CommandId.python_test_targeted
    assert adapter.command_spec(CommandId.python_test_full).command_id == CommandId.python_test_full


def test_agent_action_rejects_free_form_shell_command() -> None:
    with pytest.raises(ValidationError, match="command"):
        AgentAction.model_validate({
            "action": "run_tests",
            "reason": "bad command",
            "tool_args": {"command": "python -c 'import os'"},
        })


def test_safe_execution_environment_excludes_secret_variables() -> None:
    environment = build_safe_execution_environment()

    assert "OPENAI_API_KEY" not in environment
    assert "GITHUB_TOKEN" not in environment
    assert environment["PYTHONNOUSERSITE"] == "1"
