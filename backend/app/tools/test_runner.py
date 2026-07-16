"""测试运行工具 —— 在临时仓库中运行白名单测试命令（默认 pytest -q）。"""

from typing import Any

from app.tools.base import BaseTool
from app.models.review import CommandId
from app.tools.command_runner import build_command_executor, resolve_command_spec


class TestRunnerTool(BaseTool):
    name = "test_runner"
    description = "Run allowlisted tests in the temporary repository."

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """执行测试，返回单条 TestRunResult。"""
        repo_path = kwargs["repo_path"]
        command_id = kwargs.get("command_id", CommandId.python_test_full)
        spec = resolve_command_spec(command_id, kwargs.get("adapter_id", "python"))
        executor = kwargs.get("executor") or build_command_executor()
        result = await executor.execute(repo_path, spec)
        return {"test_results": [result.model_dump(mode="json")]}
