from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    github_token: str | None = None
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    repoguardian_model: str = "gpt-4.1-mini"
    repoguardian_provider: str = "mock"
    repoguardian_workdir: Path = Path(__file__).resolve().parent.parent.parent / ".repoguardian" / "workspaces"
    repoguardian_git_bin: str = "git"
    repoguardian_db_path: Path = Path(".repoguardian/repoguardian.db")
    repoguardian_checkpoint_db: Path = Path(".repoguardian/checkpoints.db")

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()

