"""Python 项目的第一版适配器。"""

from pathlib import Path

from app.models.review import ProjectProfile


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

    def detect(self, repo_path: Path) -> ProjectProfile | None:
        detected_files = [name for name in self._MARKERS if (repo_path / name).is_file()]
        if not detected_files and not any(repo_path.rglob("*.py")):
            return None
        return ProjectProfile(
            adapter_id=self.adapter_id,
            language="python",
            detected_files=detected_files,
        )
