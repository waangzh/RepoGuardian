"""Local browser acceptance host: real API/DB/graph, no external model or GitHub writes."""

import asyncio
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import uvicorn

from app.api import reviews, system
from app.main import app
from app.services.preview.preview_jobs import PreviewJobs
from tests.review.test_native_agents import source_service
from tests.review.test_native_lifecycle import pipeline


def main():
    directory = tempfile.TemporaryDirectory(prefix="repoguardian-browser-")
    root = Path(directory.name)
    monkeypatch = pytest.MonkeyPatch()
    source = source_service.__wrapped__(root)
    inputs = root / "pipeline"
    inputs.mkdir()
    fixture = pipeline.__wrapped__(source, inputs, monkeypatch)
    service, repository, _, _, _ = next(fixture)
    previews = PreviewJobs(service)
    monkeypatch.setattr(reviews, "get_review_service", lambda: service)
    monkeypatch.setattr(reviews, "get_preview_jobs", lambda: previews)
    monkeypatch.setattr(system, "get_review_service", lambda: service)

    async def catalog():
        return {"provider": "fixture", "default_model": "fixture", "models": [{"id": "fixture"}]}

    monkeypatch.setattr(system, "get_available_models", catalog)

    @asynccontextmanager
    async def lifespan(_app):
        worker = asyncio.create_task(service._worker.run_forever())
        previews.start()
        try:
            yield
        finally:
            await previews.close()
            service._worker.stop()
            await worker

    app.router.lifespan_context = lifespan
    try:
        uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 8189,
                    log_level="warning")
    finally:
        monkeypatch.undo()
        try:
            next(fixture)
        except StopIteration:
            pass
        directory.cleanup()


if __name__ == "__main__":
    main()
