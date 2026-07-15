"""默认的、仅包含 Python 的项目适配器注册表。"""

from app.projects.adapter import ProjectAdapterRegistry
from app.projects.python import PythonProjectAdapter


default_project_registry = ProjectAdapterRegistry([PythonProjectAdapter()])
