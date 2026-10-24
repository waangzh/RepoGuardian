"""SQLite 持久化 fixture 与最小任务/Unit 工厂。"""

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.review import ReviewTask, ReviewUnit, ReviewUnitComplexity
from app.services.review.artifact_store import LocalArtifactStore
from app.services.review.review_repository import ReviewRepository
from app.services.review.task_queue import DatabaseTaskQueue


@pytest.fixture
def persistence(tmp_path: Path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'state.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _foreign_keys(connection, _record):  # type: ignore[no-untyped-def]
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    repository = ReviewRepository(
        sessions,
        LocalArtifactStore(tmp_path / "artifacts"),
        require_migration=False,
    )
    yield repository, DatabaseTaskQueue(sessions), sessions
    engine.dispose()


def _task() -> ReviewTask:
    return ReviewTask(
        id=uuid4().hex,
        pr_url="https://github.com/acme/repo/pull/7",
        model="model-a",
    )


def _unit(fingerprint: str = "f" * 64) -> ReviewUnit:
    return ReviewUnit(
        id="ru-test",
        primary_files=["app.py"],
        fingerprint=fingerprint,
        estimated_tokens=512,
        complexity=ReviewUnitComplexity.small,
        grouping_reason="single_file",
    )
