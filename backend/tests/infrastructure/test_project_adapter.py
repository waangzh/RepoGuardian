from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models.review import AgentAction
from app.projects.python import PythonProjectAdapter


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


def test_python_adapter_detects_source_files_without_markers(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")

    profile = PythonProjectAdapter().detect(tmp_path)

    assert profile is not None
    assert profile.detected_files == []


def test_agent_action_rejects_free_form_shell_command() -> None:
    with pytest.raises(ValidationError, match="command"):
        AgentAction.model_validate({
            "action": "task_done",
            "reason": "bad command",
            "tool_args": {"command": "python -c 'import os'"},
        })
