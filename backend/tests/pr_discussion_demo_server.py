"""Ephemeral real API + SQLite browser fixture; no GitHub or model calls."""
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import uvicorn
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.api import reviews
from app.core.database import Base
from app.models.review import ReviewTask
from app.models.pr_discussion import DiscussionThread, DiscussionComment
from app.services.artifact_store import LocalArtifactStore
from app.services.review_repository import ReviewRepository
from app.services.pr_discussions import metadata, thread_input
from app.services.pr_discussion_store import DiscussionStore
from test_pr_discussions import snapshot

def main():
    with TemporaryDirectory(prefix="pr-discussion-demo-") as directory:
        root = Path(directory)
        engine = create_engine(f"sqlite:///{(root / 'demo.db').as_posix()}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        repo = ReviewRepository(sessionmaker(engine, expire_on_commit=False), LocalArtifactStore(root / "artifacts"), require_migration=False)
        fixture = json.loads((Path(__file__).parents[2] / "frontend/tests/fixtures/publication-plans.json").read_text(encoding="utf-8"))
        task = ReviewTask.model_validate(fixture["findings"]["task"])
        repo.create_task(task)
        async def seed():
            value = snapshot(task.id, body="The retry branch may return null. Author says fixed; reviewer disputes the retry path.")
            value.pr_url = task.pr_url
            value.head_sha = task.run_manifest.head_sha
            value.base_sha = task.run_manifest.effective_base_sha
            value.threads[0].comments[0].url = task.pr_url + "#discussion_r1"
            value.threads.append(DiscussionThread(id="conversation:2", source="conversation", comments=[DiscussionComment(id="comment:2", body="Please verify the compatibility behavior.")]))
            store = DiscussionStore(task.id, {"pr_url": task.pr_url, "head_sha": value.head_sha, "base_sha": value.base_sha}, repository=repo)
            store.data["snapshot"] = await store.put("snapshot", value.model_dump(mode="json"))
            store.data["input"] = await store.put("input", {"items": [thread_input(t) for t in value.threads]})
            store.data["used"] = {"inline:1": ["comment:1"]}
            store.data["rechecks"] = {"inline:1": {"discussion_id": "inline:1", "status": "still_present", "reason": "Fixture: the current retry path still dereferences null.", "evidence_ids": [], "limitations": []}, "conversation:2": {"discussion_id": "conversation:2", "status": "not_checked", "reason": "Fixture: remaining budget exhausted.", "evidence_ids": [], "limitations": []}}
            await store.save()
            task.pr_discussion = await metadata(store)
            record = task.task_runtime_report["findings"][0]
            record["verification"]["discussion_manifest_hash"] = task.pr_discussion["manifest_hash"]
            record["verification"]["discussion_matches"] = [{"discussion_id": "inline:1", "comment_id": "comment:1", "quote": "retry branch may return null", "same_subject": True, "same_trigger": True, "same_impact": True, "new_information": False, "reason": "Fixture same issue"}]
            repo.save_task(task)
        asyncio.run(seed())
        reviews.get_review_service = lambda: SimpleNamespace(get_task=repo.get_task, _repository=repo)
        app = FastAPI()
        app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5188"], allow_methods=["GET"], allow_headers=["*"])
        app.include_router(reviews.router, prefix="/api")
        @app.get("/test/task")
        def identity():
            return {"id": task.id, "discussion_stats": task.pr_discussion["stats"]}
        uvicorn.run(app, host="127.0.0.1", port=8878, log_level="warning")
        engine.dispose()

if __name__ == "__main__":
    main()
