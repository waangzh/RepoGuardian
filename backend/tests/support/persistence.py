"""Isolated business database and queue for publication safety tests."""
from pathlib import Path
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from app.core.database import Base
from app.review.store import ReviewStore
from app.services.review.task_queue import DatabaseTaskQueue


@pytest.fixture
def persistence(tmp_path: Path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'state.db').as_posix()}",
                           connect_args={"check_same_thread": False})
    @event.listens_for(engine, "connect")
    def _foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    repo = ReviewStore(sessions)
    yield repo, DatabaseTaskQueue(sessions), sessions
    engine.dispose()
