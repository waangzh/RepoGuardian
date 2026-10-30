import hashlib
import json
from datetime import timedelta

import pytest

from app.review.domain import (
    Candidate, CandidateInput, Configuration, Finding, Location, Options, Plan,
    Report, Run, Snapshot, SourceRef, VerificationOutput, utcnow,
)
from app.models.review.diff import ChangedFile, DiffHunk, DiffLine, PullRequestInfo, PullRequestRef
from app.review.findings import finding_digest
from app.services.review.artifact_store import LocalArtifactStore


def _sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source(store, snapshot, *, side, line, content):
    payload = {
        "review_id": snapshot.review_id,
        "commit_sha": snapshot.head_sha if side == "head" else snapshot.base_sha,
        "side": side,
        "path": "safe.py",
        "start_line": line,
        "end_line": line,
        "content_hash": _sha(content),
        "content": content,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    identity = _sha(encoded)
    directory = store.root / snapshot.review_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"evidence-{identity}.txt").write_text(encoded, encoding="utf-8")
    return SourceRef(id=identity, **{key: value for key, value in payload.items() if key != "content"})


@pytest.fixture
def normalized_run(tmp_path):
    base, head = "a" * 40, "b" * 40
    snapshot = Snapshot(review_id="review", repository="local/repo", repository_id=123,
        pr_number=1, declared_base_sha=base, base_sha=base, head_sha=head)
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    base_ref = _source(artifacts, snapshot, side="base", line=1, content="VALUE = 1\n")
    head_ref = _source(artifacts, snapshot, side="head", line=1, content="VALUE = 2\n")
    proposal = CandidateInput(claim="Value regresses", trigger="Call uses VALUE",
        impact="Incorrect result", source_refs=[base_ref.id, head_ref.id],
        location=Location(path="safe.py", line=1, side="head"))
    verification = VerificationOutput(verdict="confirmed", attribution="introduced",
        severity="high", reason="Fixed snapshots differ",
        source_refs=[base_ref.id, head_ref.id], author_comment="Restore the previous value.")
    candidate = Candidate(id="candidate", unit_id="unit", attempt_id="attempt", tool_call_id="tool",
        proposal=proposal, sources=[base_ref, head_ref], verification=verification,
        verification_sources=[base_ref, head_ref])
    finding = Finding(id="finding-candidate", candidate_id=candidate.id, claim=proposal.claim,
        trigger=proposal.trigger, impact=proposal.impact, severity="high", attribution="introduced",
        author_comment=verification.author_comment, location=proposal.location, sources=[base_ref, head_ref],
        publishable=True, admission_digest="pending")
    candidate.finding = finding
    finding.admission_digest = finding_digest(candidate, snapshot)
    now = utcnow()
    run = Run(id="review", status="completed", phase="report",
        pr_url="https://github.com/local/repo/pull/1",
        pr=PullRequestInfo(owner="local", repo="repo", number=1, title="fixture", body=None,
            html_url="https://github.com/local/repo/pull/1", clone_url="https://example.test/repo.git",
            base=PullRequestRef(ref="main", sha=base, repo_clone_url="https://example.test/repo.git"),
            head=PullRequestRef(ref="change", sha=head, repo_clone_url="https://example.test/repo.git")),
        snapshot=snapshot, configuration=Configuration(provider="fixture", endpoint="https://example.test",
            model="fixture", options=Options()),
        changed_files=[ChangedFile(file_path="safe.py", change_type="modified", additions=1, deletions=1,
            hunks=[DiffHunk(old_start=1, old_length=1, new_start=1, new_length=1, hunk_id="hunk",
                lines=[DiffLine(kind="deleted", content="VALUE = 1", old_line_no=1),
                       DiffLine(kind="added", content="VALUE = 2", new_line_no=1)])])],
        plan=Plan(units=[]), report=Report(version=_sha("report-v1"), status="completed",
            findings=[finding], candidates=[candidate], units=[], excluded={}),
        review_cutoff=now, hard_deadline=now + timedelta(hours=1), completed_at=now)
    return run, artifacts


@pytest.fixture
def publication_pipeline(normalized_run, tmp_path):
    from sqlalchemy import create_engine, update
    from sqlalchemy.orm import sessionmaker
    from app.core.database import Base
    from app.models.orm import ReviewTaskOrm, WorkerJobOrm
    from app.review.store import ReviewStore
    run, artifacts = normalized_run
    engine = create_engine(f"sqlite:///{tmp_path / 'publication.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    store = ReviewStore(sessions)
    store.create(run, "local", "key", "hash")
    with sessions.begin() as session:
        session.execute(update(ReviewTaskOrm).where(ReviewTaskOrm.id == run.id).values(
            status="completed", task_snapshot=run.model_dump(mode="json")))
        session.execute(update(WorkerJobOrm).values(status="completed"))
    yield run, store, artifacts
    engine.dispose()
