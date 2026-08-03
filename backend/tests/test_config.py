from pathlib import Path

from app.core.config import Settings


def test_settings_accepts_utf8_bom_env_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("\ufeffGITHUB_TOKEN=test-token\n", encoding="utf-8")

    loaded = Settings(_env_file=env_file)

    assert loaded.github_token == "test-token"
