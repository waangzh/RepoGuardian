"""全局配置 —— 从 .env 文件 / 环境变量加载，提供类型安全的 Settings 单例。"""

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置，自动读取项目根目录的 .env 文件。"""
    # ---- API 密钥 ----
    github_token: str | None = None
    openai_api_key: str | None = None

    # ---- LLM Provider ----
    openai_base_url: str = "https://api.openai.com/v1"
    repoguardian_model: str = "gpt-4.1-mini"
    repoguardian_provider: str = "openai"  # openai / deepseek / openai-compatible

    # ---- LangSmith 可观测性（默认不追踪，也不上传审查内容）----
    repoguardian_langsmith_tracing: bool = False
    langsmith_api_key: str | None = None
    langsmith_project: str = "repoguardian"
    langsmith_endpoint: str | None = None
    repoguardian_langsmith_include_content: bool = False

    # ---- 工作目录 ----
    repoguardian_workdir: Path = Path(__file__).resolve().parent.parent.parent / ".repoguardian" / "workspaces"
    repoguardian_git_bin: str = "git"

    # ---- 产品模式 ----
    # 默认审查不依赖任何执行器；验证必须由请求显式启用。
    repoguardian_default_review_mode: Literal[
        "review", "review_and_suggest", "review_suggest_and_validate"
    ] = "review"
    repoguardian_default_validation_backend: Literal["none", "local", "gvisor"] = "none"

    # ---- 受控命令执行 ----
    # reject 和 gvisor 均不会回退到宿主机。sandbox 是旧配置值的兼容别名。
    repoguardian_executor: Literal["reject", "local", "gvisor", "sandbox"] = "reject"
    repoguardian_allow_unsafe_local_execution: bool = False
    repoguardian_sandbox_network: bool = False
    repoguardian_sandbox_memory_mb: int = 512
    repoguardian_sandbox_cpus: float = 1.0
    repoguardian_sandbox_pids_limit: int = 64
    repoguardian_sandbox_timeout_seconds: int = 300
    repoguardian_sandbox_max_output_chars: int = 8_000

    # ---- 数据库 ----
    repoguardian_db_path: Path = Path(".repoguardian/repoguardian.db")
    repoguardian_checkpoint_db: Path = Path(".repoguardian/checkpoints.db")

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


# 全局配置单例
settings = Settings()
