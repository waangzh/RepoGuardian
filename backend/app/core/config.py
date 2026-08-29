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
    repoguardian_model_request_attempts: int = Field(default=2, ge=1, le=5)
    repoguardian_model_retry_backoff_seconds: float = Field(default=1.0, ge=0, le=30)
    repoguardian_model_request_timeout_seconds: float = Field(default=60, ge=1, le=600)
    repoguardian_issue_dedup_timeout_seconds: float = Field(default=30, ge=0.1, le=300)
    repoguardian_report_purpose_timeout_seconds: float = Field(default=15, ge=0.1, le=120)
    # JSON: {"provider:model":{"input":0.40,"output":1.60,"cached_input":0.10}}
    # Rates are USD per one million tokens. Missing entries keep observed cost unknown.
    repoguardian_model_pricing_json: str = "{}"

    # ---- LangSmith 可观测性（默认不追踪，也不上传审查内容）----
    repoguardian_langsmith_tracing: bool = False
    langsmith_api_key: str | None = None
    langsmith_project: str = "repoguardian"
    langsmith_endpoint: str | None = None
    repoguardian_langsmith_include_content: bool = False

    # ---- 工作目录 ----
    repoguardian_workdir: Path = Path(__file__).resolve().parent.parent.parent / ".repoguardian" / "workspaces"
    repoguardian_git_bin: str = "git"
    repoguardian_git_timeout_seconds: int = Field(default=900, ge=30, le=86_400)

    # ---- 产品模式 ----
    # 默认审查不依赖任何执行器；验证必须由请求显式启用。
    repoguardian_default_review_mode: Literal[
        "review", "review_and_suggest", "review_suggest_and_validate"
    ] = "review"
    repoguardian_default_validation_backend: Literal[
        "none", "user_runner", "project_ci", "gvisor"
    ] = "none"
    repoguardian_default_validation_profile: str = "unit"

    # ---- Project CI / GitHub Actions ----
    # 仓库必须显式安装该 workflow；默认不启用 Project CI。
    repoguardian_project_ci_workflow: str | None = None
    repoguardian_project_ci_ref: str = "main"
    repoguardian_project_ci_workflow_name: str = "RepoGuardian Validation"
    repoguardian_project_ci_profiles: str = "unit=unit"
    repoguardian_project_ci_allow_fork: bool = False
    repoguardian_project_ci_timeout_seconds: int = Field(default=3600, ge=60, le=86_400)
    repoguardian_project_ci_poll_interval_seconds: int = Field(default=30, ge=5, le=300)
    repoguardian_project_ci_max_patch_input_bytes: int = Field(
        default=48_000, ge=1_024, le=60_000
    )
    repoguardian_github_webhook_secret: str | None = None

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
    # 已从 Production Review Plane 废弃，仅保留旧开发工具/配置解析兼容性。
    # reject 和 gvisor 均不会回退到宿主机；sandbox 是旧配置值的兼容别名。
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
    repoguardian_worker_poll_seconds: float = Field(default=0.25, ge=0.05, le=60)
    repoguardian_worker_lease_seconds: int = Field(default=300, ge=5, le=86_400)
    repoguardian_worker_max_attempts: int = Field(default=3, ge=1, le=20)
    repoguardian_human_timeout_seconds: int = Field(default=86_400, ge=60, le=2_592_000)
    repoguardian_human_timeout_policy: Literal["fail", "cancel"] = "fail"
    repoguardian_human_answer_token: str | None = None
    repoguardian_artifact_inline_max_bytes: int = Field(default=64_000, ge=1_024, le=1_000_000)
    repoguardian_artifact_dir: Path = (
        Path(__file__).resolve().parent.parent.parent / ".repoguardian" / "artifacts"
    )
    repoguardian_retention_days: int = Field(default=30, ge=1, le=3650)
    repoguardian_maintenance_interval_seconds: int = Field(
        default=3_600, ge=60, le=86_400
    )
    repoguardian_orphan_workspace_ttl_seconds: int = Field(
        default=604_800, ge=3_600, le=31_536_000
    )
    repoguardian_checkpoint_vacuum_min_bytes: int = Field(
        default=64 * 1024 * 1024, ge=0
    )
    repoguardian_checkpoint_vacuum_min_ratio: float = Field(
        default=0.2, ge=0.0, le=1.0
    )

    # ---- 可复用结论版本 ----
    repoguardian_config_version: str = "6A-v2"
    repoguardian_prompt_version: str = "review-v4"
    repoguardian_rule_version: str = "rules-v2"
    repoguardian_tool_schema_version: str = "tools-v3"
    repoguardian_review_policy_version: str = "review-policy-v1"
    repoguardian_patch_policy_version: str = "patch-policy-v1"
    repoguardian_allow_cross_model_reuse: bool = False

    # Windows 编辑器常会给 UTF-8 .env 写入 BOM；utf-8-sig 会安全移除 BOM，
    # 避免首个配置键被解析成 ``\ufeffGITHUB_TOKEN`` 之类的隐藏键名。
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8-sig",
        extra="ignore",
    )


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


def registered_project_ci_profiles() -> dict[str, str]:
    """解析 Project CI profile -> 必须成功的检查名称映射。"""
    profiles: dict[str, str] = {}
    for item in settings.repoguardian_project_ci_profiles.split(","):
        profile_id, separator, check_name = item.strip().partition("=")
        if separator and profile_id and check_name:
            profiles[profile_id] = check_name
    return profiles
