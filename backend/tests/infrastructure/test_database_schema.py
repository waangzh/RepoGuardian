from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select

from app.core import database
from app.models.orm import PreviewJobOrm


def test_cache_cleanup_migrates_new_and_existing_schema_without_losing_jobs(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'business.db').as_posix()}")
    monkeypatch.setattr(database, "sync_engine", engine)
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            connection.execute(PreviewJobOrm.__table__.insert().values(
                id="retained-job", request={"pr_url": "https://github.com/local/repo/pull/1"},
                status="completed",
            ))
            connection.commit()
            assert database.schema_is_current()
            assert "preview_input_cache" not in inspect(connection).get_table_names()

            command.downgrade(config, "20261027_0001")
            connection.commit()
            assert not database.schema_is_current()
            assert "preview_input_cache" in inspect(connection).get_table_names()

            command.upgrade(config, "head")
            connection.commit()
            assert database.schema_is_current()
            assert "preview_input_cache" not in inspect(connection).get_table_names()
            assert connection.scalar(select(PreviewJobOrm.id)) == "retained-job"

            command.downgrade(config, "base")
            connection.commit()
            assert inspect(connection).get_table_names() == ["alembic_version"]
    finally:
        engine.dispose()
