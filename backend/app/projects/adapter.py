"""项目适配器协议与服务端注册表。"""

from pathlib import Path
from typing import Protocol

from app.models.review import CommandId, CommandSpec, ProjectProfile


class ProjectAdapter(Protocol):
    """将项目识别和受控验证命令绑定到同一个语言适配器。"""

    adapter_id: str

    def detect(self, repo_path: Path) -> ProjectProfile | None:
        """识别项目；未匹配时返回 None。"""

    def command_spec(self, command_id: CommandId) -> CommandSpec:
        """只返回本适配器预注册的命令定义。"""


class ProjectAdapterRegistry:
    """按固定顺序检测适配器，避免从外部仓库加载可执行配置。"""

    def __init__(self, adapters: list[ProjectAdapter]) -> None:
        self._adapters = tuple(adapters)
        self._by_id = {adapter.adapter_id: adapter for adapter in adapters}

    def detect(self, repo_path: Path) -> ProjectProfile | None:
        for adapter in self._adapters:
            profile = adapter.detect(repo_path)
            if profile is not None:
                return profile
        return None

    def get(self, adapter_id: str) -> ProjectAdapter | None:
        return self._by_id.get(adapter_id)
