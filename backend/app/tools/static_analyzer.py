from typing import Any

from app.tools.base import BaseTool
from app.tools.command_runner import run_command


class StaticAnalyzerTool(BaseTool):
    name = "static_analyzer"
    description = "Run allowlisted static analysis commands in the temporary repository."

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        repo_path = kwargs["repo_path"]
        command = kwargs.get("command")
        result = await run_command(
            repo_path=repo_path,
            command=command,
            default="ruff check .",
            tool=self.name,
            timeout_seconds=kwargs.get("timeout_seconds", 60),
        )
        return {"static_results": [result.model_dump(mode="json")]}
