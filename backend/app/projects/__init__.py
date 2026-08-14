"""项目适配器注册表。"""

from app.projects.adapter import ProjectAdapter, ProjectAdapterRegistry, ProjectValidationAdapter
from app.projects.python import PythonProjectAdapter

__all__ = [
    "ProjectAdapter",
    "ProjectAdapterRegistry",
    "ProjectValidationAdapter",
    "PythonProjectAdapter",
]
