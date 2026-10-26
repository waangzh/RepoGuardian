import pytest

from app.languages import isolation, parser_cache

collect_ignore = ["fixtures"]

pytest_plugins = ["tests.support.persistence"]


@pytest.fixture(autouse=True)
def isolated_parser_cache(tmp_path, monkeypatch):
    directory = tmp_path / ".repoguardian"
    monkeypatch.setattr(parser_cache, "_cache_path", lambda: directory / "parser-cache.db")
    monkeypatch.setattr(isolation, "_failure_directory", lambda: directory / "parser-failures")
    isolation._RECENT_FAILURES.clear()
    yield
    isolation._RECENT_FAILURES.clear()
