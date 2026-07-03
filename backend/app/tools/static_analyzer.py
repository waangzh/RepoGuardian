"""静态分析工具 —— 在临时仓库中运行白名单静态分析命令（默认 ruff check）。"""

from typing import Any

from app.tools.base import BaseTool
from app.tools.command_runner import run_command


class StaticAnalyzerTool(BaseTool):
    name = "static_analyzer"
    description = "Run allowlisted static analysis commands in the temporary repository."

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """执行静态分析，返回单条 TestRunResult。"""
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
