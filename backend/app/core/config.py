"""全局配置 —— 从 .env 文件 / 环境变量加载，提供类型安全的 Settings 单例。"""

from pathlib import Path
from typing import Literal

from pydantic import Field
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
    repoguardian_default_validation_backend: Literal[
        "none", "user_runner", "project_ci", "gvisor"
    ] = "none"
    repoguardian_default_validation_profile: str = "unit"

    # ---- User Runner 协议 ----
    # profile 仅映射服务端注册的逻辑 command_id，不是 shell 字符串。
    repoguardian_runner_profiles: str = "unit=project_unit_tests,lint=project_lint"
    repoguardian_runner_registration_token: str | None = None
    repoguardian_runner_claim_timeout_seconds: int = Field(default=600, ge=30, le=86_400)
    repoguardian_runner_request_timeout_seconds: int = Field(default=7200, ge=60, le=604_800)
    repoguardian_runner_max_log_summary_chars: int = Field(default=8_000, ge=0, le=20_000)

    # ---- Review Unit Planner / 调度 ----
    repoguardian_review_unit_concurrency: int = 4
    repoguardian_review_unit_timeout_seconds: int = 180
    repoguardian_review_unit_small_max_changed_lines: int = 30
    repoguardian_review_unit_large_min_changed_lines: int = 400
    repoguardian_review_unit_max_lines_per_read: int = 240
    repoguardian_review_unit_max_search_results: int = 12

    # ---- Issue 确定性策略与独立验证 ----
    repoguardian_issue_verifier_enabled: bool = True
    repoguardian_issue_verifier_fail_mode: Literal["needs_human", "candidate"] = "needs_human"
    repoguardian_issue_verifier_max_calls_per_unit: int = Field(default=5, ge=0, le=100)

    # ---- 阶段 4 候选补丁资格与确定性大小上限 ----
    repoguardian_patch_confidence_threshold: float = Field(default=0.8, ge=0, le=1)
    repoguardian_patch_max_files: int = Field(default=3, ge=1, le=10)
    repoguardian_patch_max_changed_lines: int = Field(default=80, ge=1, le=1_000)

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


def registered_runner_profiles() -> dict[str, str]:
    """解析服务端预注册 profile；值始终是逻辑 command_id。"""
    profiles: dict[str, str] = {}
    for item in settings.repoguardian_runner_profiles.split(","):
        profile_id, separator, command_id = item.strip().partition("=")
        if separator and profile_id and command_id:
            profiles[profile_id] = command_id
    if not profiles:
        raise ValueError("REPOGUARDIAN_RUNNER_PROFILES must register at least one profile")
    return profiles
