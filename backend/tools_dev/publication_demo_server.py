"""本地浏览器集成验收：临时 SQLite + 真实 API/Worker + MockTransport，无 GitHub 网络请求。"""
import json
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import sys
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.api import publications
from app.core.database import Base
from app.models.review import ReviewTask
from app.services.review.artifact_store import LocalArtifactStore
from app.services.review.review_repository import ReviewRepository
from app.services.publication.publication_repository import PublicationRepository
from app.services.publication.publication_service import PublicationService
from app.services.review.task_queue import DatabaseTaskQueue
from app.tools.github.github_publisher import GitHubPublisher
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from test_sticky_summary import FakeGitHub
from tests.support.publication_fixtures import publish_config


def main():
    with TemporaryDirectory(prefix="publication-demo-") as directory:
        root = Path(directory)
        engine = create_engine(f"sqlite:///{(root / 'demo.db').as_posix()}", connect_args={"check_same_thread": False})
        @event.listens_for(engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
        Base.metadata.create_all(engine)
        sessions = sessionmaker(engine, expire_on_commit=False)
        review_repo = ReviewRepository(sessions, LocalArtifactStore(root / "artifacts"), require_migration=False)
        fixtures = Path(__file__).parents[2] / "frontend/tests/fixtures/publication-plans.json"
        task = ReviewTask.model_validate(json.loads(fixtures.read_text(encoding="utf-8"))["findings"]["task"])
        review_repo.create_task(task)
        fake = FakeGitHub()
        github = GitHubPublisher("fixture-no-network", transport=httpx.MockTransport(fake))
        config = publish_config()
        service = PublicationService(PublicationRepository(sessions), github, config, review_repo.get_task, DatabaseTaskQueue(sessions))
        publications.settings = config
        publications.get_publication_service = lambda: service
        @asynccontextmanager
        async def lifespan(app):
            service.start()
            yield
            await service.close()
        app = FastAPI(lifespan=lifespan)
        app.include_router(publications.router, prefix="/api")
        @app.get("/test/snapshot")
        async def snapshot():
            return {"summaries": len(fake.comments), "reviews": len(fake.reviews), "inline": len(fake.inline),
                    "writes": fake.writes}
        uvicorn.run(app, host="127.0.0.1", port=8767, log_level="warning")
        engine.dispose()


if __name__ == "__main__":
    main()
