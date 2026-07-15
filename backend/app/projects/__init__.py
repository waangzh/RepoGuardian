"""项目适配器注册表。"""

from app.projects.adapter import ProjectAdapter, ProjectAdapterRegistry
from app.projects.python import PythonProjectAdapter

__all__ = ["ProjectAdapter", "ProjectAdapterRegistry", "PythonProjectAdapter"]
