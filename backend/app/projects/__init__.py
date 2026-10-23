"""项目适配器注册表。"""

from app.projects.adapter import ProjectAdapterRegistry, ProjectValidationAdapter
from app.projects.python import PythonProjectAdapter

__all__ = [
    "ProjectAdapterRegistry",
    "ProjectValidationAdapter",
    "PythonProjectAdapter",
]
