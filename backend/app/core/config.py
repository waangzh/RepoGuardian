"""全局配置 —— 从 .env 文件 / 环境变量加载，提供类型安全的 Settings 单例。"""

import os
import re
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置，自动读取项目根目录的 .env 文件。"""
    # ---- API 密钥 ----
    github_token: str | None = None
    repoguardian_github_publish_enabled: bool = False
    repoguardian_github_publish_token: SecretStr | None = None
    repoguardian_publication_api_token: SecretStr | None = None
    repoguardian_github_publish_allowed_repositories: str = ""
    repoguardian_github_publish_publisher_id: int | None = Field(default=None, ge=1)
    # Fine-grained PAT 没有通用的自身权限查询端点，部署者须核对 PR write grant。
    repoguardian_github_publish_pull_requests_write_confirmed: bool = False
    repoguardian_github_publish_max_inline: int = Field(default=30, ge=1, le=100)
    repoguardian_github_publish_batch_size: int = Field(default=15, ge=1, le=30)

    @model_validator(mode="after")
    def validate_publication_config(self):
        if self.repoguardian_github_publish_enabled:
            token = self.repoguardian_publication_api_token
            if (not self.repoguardian_github_publish_token or not token or len(token.get_secret_value()) < 32
                    or not self.repoguardian_github_publish_publisher_id
                    or not self.repoguardian_github_publish_pull_requests_write_confirmed
                    or not self.repoguardian_github_publish_allowed_repositories.strip()):
                raise ValueError("publication requires dedicated credentials, publisher ID, allowlist and confirmed PR write grant")
            if any(not value.strip().isdigit() or int(value) < 1 for value in self.repoguardian_github_publish_allowed_repositories.split(",")):
                raise ValueError("publication allowlist must contain numeric GitHub repository IDs")
        return self
    openai_api_key: str | None = None

    # ---- LLM Provider ----
    openai_base_url: str = "https://api.openai.com/v1"
    repoguardian_model: str = "gpt-4.1-mini"
    repoguardian_provider: str = "openai"  # openai / deepseek / openai-compatible
    repoguardian_model_request_timeout_seconds: float = Field(default=60, ge=1, le=600)

    repoguardian_pr_discussions_enabled: bool = True


    # ---- LangSmith：显式启用会上传模型输入、源码工具结果与输出 ----
    repoguardian_langsmith_tracing: bool = False
    langsmith_api_key: str | None = None
    langsmith_project: str = "repoguardian"
    langsmith_endpoint: str | None = None

    # ---- 工作目录 ----
    repoguardian_workdir: Path = Path(__file__).resolve().parent.parent.parent / ".repoguardian" / "workspaces"
    repoguardian_git_bin: str = "git"
    repoguardian_git_timeout_seconds: int = Field(default=900, ge=30, le=86_400)

    # ---- 产品模式 ----
    # 只读静态审查；不提供动态执行入口。
    repoguardian_github_webhook_secret: str | None = None

    # ---- 原生 Agent 派发与有界运行 ----
    repoguardian_review_concurrency: int = Field(default=4, ge=1, le=16)
    repoguardian_incremental_review_webhook_enabled: bool = False
    repoguardian_incremental_review_webhook_repositories: str = ""
    repoguardian_incremental_review_max_runs_per_hour: int = Field(default=4, ge=1, le=100)
    repoguardian_review_timeout_seconds: float = Field(default=1800, ge=1, le=86_400)
    # 调用次数只作熔断；调查以整轮 Token 和期限为主，深度不再覆盖此值。
    repoguardian_agent_max_calls: int = Field(default=48, ge=1, le=100)
    repoguardian_agent_max_tool_calls: int = Field(default=80, ge=1, le=500)
    repoguardian_agent_output_tokens: int = Field(default=4096, ge=256, le=32768)
    repoguardian_review_max_model_calls: int = Field(default=64, ge=1, le=1000)
    repoguardian_review_max_input_tokens: int = Field(default=500_000, ge=1000, le=10_000_000)
    repoguardian_review_max_output_tokens: int = Field(default=262144, ge=1024, le=10_000_000)
    repoguardian_model_context_tokens: int = Field(default=64000, ge=8192, le=1_000_000)

    # ---- 数据库 ----
    repoguardian_db_path: Path = Path(".repoguardian/reviews.db")
    repoguardian_worker_poll_seconds: float = Field(default=0.25, ge=0.05, le=60)
    repoguardian_worker_lease_seconds: int = Field(default=300, ge=5, le=86_400)
    repoguardian_worker_max_attempts: int = Field(default=3, ge=1, le=20)
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
    repoguardian_prepared_workspace_ttl_seconds: int = Field(default=3_600, ge=900, le=86_400)
    repoguardian_prepared_workspace_max_entries: int = Field(default=4, ge=0, le=100)
    repoguardian_prepared_workspace_max_bytes: int = Field(default=4 * 1024**3, ge=0)


    # Windows 编辑器常会给 UTF-8 .env 写入 BOM；utf-8-sig 会安全移除 BOM，
    # 避免首个配置键被解析成 ``\ufeffGITHUB_TOKEN`` 之类的隐藏键名。
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8-sig",
        extra="ignore",
        hide_input_in_errors=True,
    )


_EDITABLE_ENV_KEYS = {
    "REPOGUARDIAN_RETENTION_DAYS",
    "OPENAI_BASE_URL",
    "REPOGUARDIAN_MODEL",
}


def persist_editable_settings(values: dict[str, object]) -> None:
    """更新白名单配置并原子写回 backend/.env，不触碰秘密字段。"""
    env_path = Path(__file__).resolve().parents[2] / ".env"
    existing = env_path.read_text(encoding="utf-8-sig") if env_path.exists() else ""
    lines = existing.splitlines()
    updates = {key: str(value) for key, value in values.items() if key in _EDITABLE_ENV_KEYS}
    for key, value in updates.items():
        if "\n" in value or "\r" in value:
            raise ValueError(f"{key} contains a line break")
        pattern = re.compile(rf"^{re.escape(key)}=.*$")
        for index, line in enumerate(lines):
            if pattern.match(line):
                lines[index] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = env_path.with_name(env_path.name + ".tmp")
    temp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(temp_path, env_path)


# 全局配置单例
settings = Settings()
