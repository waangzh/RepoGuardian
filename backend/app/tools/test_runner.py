from typing import Any

from app.tools.base import BaseTool
from app.tools.command_runner import run_command


class TestRunnerTool(BaseTool):
    name = "test_runner"
    description = "Run allowlisted tests in the temporary repository."

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        repo_path = kwargs["repo_path"]
        command = kwargs.get("command")
        result = await run_command(
            repo_path=repo_path,
            command=command,
            default="python -m pytest -q",
            tool=self.name,
            timeout_seconds=kwargs.get("timeout_seconds", 120),
        )
        return {"test_results": [result.model_dump(mode="json")]}
