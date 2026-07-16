"""静态分析工具 —— 在临时仓库中运行白名单静态分析命令（默认 ruff check）。"""

from typing import Any

from app.tools.base import BaseTool
from app.models.review import CommandId
from app.tools.command_runner import build_command_executor, resolve_command_spec


class StaticAnalyzerTool(BaseTool):
    name = "static_analyzer"
    description = "Run allowlisted static analysis commands in the temporary repository."

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """执行静态分析，返回单条 TestRunResult。"""
        repo_path = kwargs["repo_path"]
        command_id = kwargs.get("command_id", CommandId.python_static_default)
        spec = resolve_command_spec(command_id, kwargs.get("adapter_id", "python"))
        executor = kwargs.get("executor") or build_command_executor()
        result = await executor.execute(repo_path, spec)
        return {"static_results": [result.model_dump(mode="json")]}
