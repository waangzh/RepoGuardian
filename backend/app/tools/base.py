"""工具基类 —— 所有工具的抽象父类。"""

from abc import ABC, abstractmethod
from typing import Any


class BaseTool(ABC):
    """工具抽象基类，子类需提供 name/description 并实现 execute()。"""
    name: str = ""
    description: str = ""

    @abstractmethod
    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """执行工具逻辑，返回字典结果。"""
        pass
