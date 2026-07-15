"""Python 项目的第一版适配器。"""

import sys
from pathlib import Path

from app.models.review import CommandId, CommandSpec, ProjectProfile


class PythonProjectAdapter:
    """仅识别文件布局；不读取或执行仓库中的配置文本。"""

    adapter_id = "python"
    _MARKERS = (
        "pyproject.toml",
        "requirements.txt",
        "poetry.lock",
        "uv.lock",
        "Pipfile.lock",
        "pytest.ini",
        "tox.ini",
        "setup.cfg",
    )
    _VALIDATION_COMMANDS = (
        CommandId.python_test_collect,
        CommandId.python_test_full,
    )
    _COMMAND_SPECS = {
        CommandId.python_static_default: CommandSpec(
            command_id=CommandId.python_static_default,
            argv=(sys.executable, "-m", "ruff", "check", "."),
            tool="static_analyzer",
            timeout_seconds=60,
        ),
        CommandId.python_test_collect: CommandSpec(
            command_id=CommandId.python_test_collect,
            argv=(sys.executable, "-m", "pytest", "--collect-only", "-q"),
            tool="test_runner",
            timeout_seconds=120,
        ),
        CommandId.python_test_targeted: CommandSpec(
            command_id=CommandId.python_test_targeted,
            argv=(sys.executable, "-m", "pytest", "-q"),
            tool="test_runner",
            timeout_seconds=120,
        ),
        CommandId.python_test_full: CommandSpec(
            command_id=CommandId.python_test_full,
            argv=(sys.executable, "-m", "pytest", "-q"),
            tool="test_runner",
            timeout_seconds=120,
        ),
    }

    def detect(self, repo_path: Path) -> ProjectProfile | None:
        detected_files = [name for name in self._MARKERS if (repo_path / name).is_file()]
        if not detected_files and not any(repo_path.rglob("*.py")):
            return None
        return ProjectProfile(
            adapter_id=self.adapter_id,
            language="python",
            detected_files=detected_files,
            validation_command_ids=list(self._VALIDATION_COMMANDS),
        )

    def command_spec(self, command_id: CommandId) -> CommandSpec:
        try:
            return self._COMMAND_SPECS[command_id]
        except KeyError as exc:
            raise ValueError(f"command_id is not registered for Python: {command_id.value}") from exc
