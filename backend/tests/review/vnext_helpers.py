from datetime import timedelta
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.review.diff import PullRequestInfo
from app.review.domain import (
    Candidate, CandidateInput, Configuration, Limits, Options, Plan, Run, Snapshot,
    SourceRef, Unit, utcnow,
)
from app.review.store import ReviewStore
from app.services.review.task_queue import DatabaseTaskQueue


def setup_run(tmp_path, *, calls=20, input_tokens=100000, concurrency=2):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}",
                          connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    store = ReviewStore(sessions)
    now, run_id = utcnow(), uuid4().hex
    pr = PullRequestInfo(owner="o", repo="r", number=1, title="Change", html_url="https://github.com/o/r/pull/1",
        clone_url="https://github.com/o/r.git",
        base={"ref": "main", "sha": "a" * 40, "repo_clone_url": "https://github.com/o/r.git"},
        head={"ref": "branch", "sha": "b" * 40, "repo_clone_url": "https://github.com/o/r.git"})
    run = Run(id=run_id, pr_url=pr.html_url, pr=pr,
        snapshot=Snapshot(review_id=run_id, repository="o/r", repository_id=1, pr_number=1,
            declared_base_sha="a" * 40, base_sha="a" * 40, head_sha="b" * 40),
        configuration=Configuration(provider="test", endpoint="http://localhost", model="fake",
            options=Options(limits=Limits(total_model_calls=calls, total_input_tokens=input_tokens,
                                          concurrency=concurrency))),
        changed_files=[], plan=Plan(units=[Unit(id="u1", paths=["a.py"], required_hunk_ids=["h1"],
            risk="low", focus=["correctness"], estimated_input_tokens=100)]),
        review_cutoff=now + timedelta(minutes=20), hard_deadline=now + timedelta(minutes=30))
    store.create(run, "local", "key", "hash")
    queue = DatabaseTaskQueue(sessions)
    job = queue.claim(worker_id="worker", lease_seconds=600, kinds=("review",))
    lease = {"job_id": job.id, "owner": job.lease_owner, "attempt": job.attempts}
    store.begin(run.id, lease)
    return store, run, lease, sessions


def submit_candidate(store, run, lease, work):
    ref = SourceRef(id="c" * 64, review_id=run.id, commit_sha=run.snapshot.head_sha,
        side="head", path="a.py", start_line=1, end_line=1, content_hash="d" * 64)
    store.deliver(run.id, work, ["h1"], {}, lease, source_ids=[ref.id])
    candidate = Candidate(id=uuid4().hex, unit_id=work.unit_id, attempt_id=work.attempt_id,
        tool_call_id="call1", proposal=CandidateInput(claim="failure", trigger="input", impact="loss",
                                                     source_refs=["S1"]), sources=[ref])
    store.submit(run.id, work, candidate, lease)
    return candidate
