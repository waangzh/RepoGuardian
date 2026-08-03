import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

import pytest
from fastapi import HTTPException
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.api import reviews as reviews_api
from app.core.database import Base
from app.models.orm import PatchOrm, ReviewTaskOrm, SideEffectOrm, WorkerJobOrm, utcnow
from app.models.persistence import HumanRequestAnswer, HumanRequestStatus
from app.models.review import (
    ChangedFile,
    DiffHunk,
    HumanReviewRequest,
    PatchProposal,
    ReviewTask,
    ReviewUnit,
    ReviewUnitComplexity,
    ReviewUnitResult,
    ReviewUnitStatus,
)
from app.services.artifact_store import LocalArtifactStore
from app.services.fingerprints import patch_fingerprint, validation_fingerprint
from app.services.review_planner import DeterministicReviewPlanner
from app.services.review_repository import ReviewRepository
from app.services.task_queue import DatabaseTaskQueue


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


def test_task_survives_repository_restart_and_api_does_not_need_memory_dict(persistence) -> None:
    repository, _, sessions = persistence
    task = _task()
    repository.create_task(task)

    restarted = ReviewRepository(
        sessions, repository._artifacts, require_migration=False  # type: ignore[attr-defined]
    )
    loaded = restarted.get_task(task.id)
    assert loaded is not None and loaded.id == task.id
    assert restarted.list_tasks(status="queued", page=1, page_size=10).total == 1


def test_completed_unit_reuses_but_failed_unit_and_version_mismatch_do_not(persistence) -> None:
    repository, _, _ = persistence
    source = _task()
    repository.create_task(source)
    unit = _unit()
    completed = ReviewUnitResult(review_unit_id=unit.id, status=ReviewUnitStatus.completed)
    repository.record_unit_result(task_id=source.id, unit=unit, result=completed)

    matched = repository.find_reusable_unit(
        fingerprint=unit.fingerprint,
        model="model-a",
        provider=settings.repoguardian_provider,
        prompt_version=settings.repoguardian_prompt_version,
        rule_version=settings.repoguardian_rule_version,
        tool_schema_version=settings.repoguardian_tool_schema_version,
        review_policy_version=settings.repoguardian_review_policy_version,
    )
    assert matched is not None
    assert repository.find_reusable_unit(
        fingerprint=unit.fingerprint,
        model="model-b",
        provider=settings.repoguardian_provider,
        prompt_version=settings.repoguardian_prompt_version,
        rule_version=settings.repoguardian_rule_version,
        tool_schema_version=settings.repoguardian_tool_schema_version,
        review_policy_version=settings.repoguardian_review_policy_version,
    ) is None
    assert repository.find_reusable_unit(
        fingerprint=unit.fingerprint,
        model="model-a",
        provider=settings.repoguardian_provider,
        prompt_version="new-prompt",
        rule_version=settings.repoguardian_rule_version,
        tool_schema_version=settings.repoguardian_tool_schema_version,
        review_policy_version=settings.repoguardian_review_policy_version,
    ) is None

    failed_task = _task()
    repository.create_task(failed_task)
    failed_unit = _unit("e" * 64).model_copy(update={"id": "ru-failed"})
    repository.record_unit_result(
        task_id=failed_task.id,
        unit=failed_unit,
        result=ReviewUnitResult(
            review_unit_id=failed_unit.id,
            status=ReviewUnitStatus.failed,
            error="retry me",
        ),
    )
    assert repository.find_reusable_unit(
        fingerprint=failed_unit.fingerprint,
        model="model-a",
        provider=settings.repoguardian_provider,
        prompt_version=settings.repoguardian_prompt_version,
        rule_version=settings.repoguardian_rule_version,
        tool_schema_version=settings.repoguardian_tool_schema_version,
        review_policy_version=settings.repoguardian_review_policy_version,
    ) is None


def test_unit_fingerprint_changes_with_diff_prompt_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    changed = [ChangedFile(
        file_path="app.py",
        change_type="modified",
        additions=1,
        deletions=1,
        hunks=[DiffHunk(
            old_start=1, old_length=1, new_start=1, new_length=1,
            added_lines=[{"line_no": 1, "content": "new"}],
            removed_lines=[{"line_no": 1, "content": "old"}],
        )],
    )]
    planner = DeterministicReviewPlanner()
    first = planner.plan(changed, base_sha="b", head_sha="h", model="m1").review_units[0]
    changed_head = planner.plan(changed, base_sha="b", head_sha="h2", model="m1").review_units[0]
    changed_model = planner.plan(changed, base_sha="b", head_sha="h", model="m2").review_units[0]
    monkeypatch.setattr(settings, "repoguardian_prompt_version", "review-v2")
    changed_prompt = planner.plan(changed, base_sha="b", head_sha="h", model="m1").review_units[0]
    assert len({first.fingerprint, changed_head.fingerprint, changed_model.fingerprint,
                changed_prompt.fingerprint}) == 4


def test_patch_and_validation_fingerprints_bind_correct_inputs() -> None:
    diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n"
    patch_a, hash_a = patch_fingerprint(
        head_sha="head-a", issue_evidence_hash="evidence", unified_diff=diff,
        patch_policy_version="policy-1",
    )
    patch_b, _ = patch_fingerprint(
        head_sha="head-b", issue_evidence_hash="evidence", unified_diff=diff,
        patch_policy_version="policy-1",
    )
    assert patch_a != patch_b
    assert validation_fingerprint(
        patch_hash=hash_a, backend="user_runner", validation_profile="unit",
        environment_fingerprint="linux-py312",
    ) != validation_fingerprint(
        patch_hash="other", backend="user_runner", validation_profile="unit",
        environment_fingerprint="linux-py312",
    )


def test_large_patch_is_externalized_and_bound_to_head(persistence, monkeypatch) -> None:
    repository, _, sessions = persistence
    monkeypatch.setattr(settings, "repoguardian_artifact_inline_max_bytes", 1024)
    task = _task()
    issue_id = "issue-1"
    patch = PatchProposal(
        issue_ids=[issue_id],
        title="修复",
        rationale="修复错误",
        unified_diff="--- a/app.py\n+++ b/app.py\n" + "+x\n" * 600,
        touched_files=["app.py"],
        risk="low",
        head_sha="head-a",
    )
    task.patches = [patch]
    repository.create_task(task)
    repository.save_task(task)
    with sessions() as session:
        row = session.scalar(select(PatchOrm).where(PatchOrm.patch_id == patch.id))
        assert row is not None
        assert row.head_sha == "head-a"
        assert row.unified_diff is None
        assert row.diff_artifact_uri and row.diff_artifact_uri.startswith("file:///")


def test_human_request_answer_is_idempotent_and_cancel_blocks_resume(persistence) -> None:
    repository, queue, _ = persistence
    task = _task()
    repository.create_task(task)
    request = repository.create_human_request(
        task_id=task.id,
        request=HumanReviewRequest(
            missing_information=["业务规则"],
            known_evidence=["代码存在两个合理解释"],
            questions=["应采用哪个解释？"],
            prohibited_operations=["不得自动修复"],
        ),
        reason="ambiguous",
    )
    answer = HumanRequestAnswer(option_id="provide_information", text="采用百分比语义")
    first, replay = repository.answer_human_request(
        task_id=task.id, request_id=request.request_id, answer=answer, answered_by="alice"
    )
    second, second_replay = repository.answer_human_request(
        task_id=task.id, request_id=request.request_id, answer=answer, answered_by="alice"
    )
    assert first.status == HumanRequestStatus.answered
    assert replay is False and second_replay is True and second.answer == first.answer

    other = _task()
    repository.create_task(other)
    pending = repository.create_human_request(
        task_id=other.id,
        request=HumanReviewRequest(
            missing_information=["规则"], known_evidence=["证据"],
            questions=["继续吗？"], prohibited_operations=["禁止修复"],
        ),
        reason="blocked",
    )
    assert repository.cancel_task(other.id)
    queue.cancel_for_task(other.id)
    with pytest.raises(ValueError, match="cancelled"):
        repository.answer_human_request(
            task_id=other.id,
            request_id=pending.request_id,
            answer=HumanRequestAnswer(option_id="provide_information", text="继续"),
            answered_by="alice",
        )


@pytest.mark.asyncio
async def test_api_reads_persistence_and_rejects_unauthorized_human_answer(
    persistence, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, _, _ = persistence
    task = _task()
    repository.create_task(task)
    request = repository.create_human_request(
        task_id=task.id,
        request=HumanReviewRequest(
            missing_information=["规则"], known_evidence=["证据"],
            questions=["如何继续？"], prohibited_operations=["禁止修复"],
        ),
        reason="human",
    )

    class PersistentOnlyService:
        def get_task(self, task_id: str):
            return repository.get_task(task_id)

        def answer_human_request(self, task_id, request_id, answer, *, answered_by):
            return repository.answer_human_request(
                task_id=task_id,
                request_id=request_id,
                answer=answer,
                answered_by=answered_by,
            )

    service = PersistentOnlyService()
    monkeypatch.setattr(reviews_api, "get_review_service", lambda: service)
    monkeypatch.setattr(settings, "repoguardian_human_answer_token", "secret-token")
    loaded = await reviews_api.get_review(task.id)
    assert loaded.id == task.id
    with pytest.raises(HTTPException) as denied:
        await reviews_api.answer_human_request(
            task.id,
            request.request_id,
            HumanRequestAnswer(option_id="provide_information", text="继续"),
            authorization="Bearer wrong",
            actor="mallory",
        )
    assert denied.value.status_code == 403
    accepted = await reviews_api.answer_human_request(
        task.id,
        request.request_id,
        HumanRequestAnswer(option_id="provide_information", text="继续"),
        authorization="Bearer secret-token",
        actor="alice",
    )
    assert accepted.request.answered_by == "alice"
    assert accepted.idempotent_replay is False


def test_two_workers_cannot_claim_same_job_and_expired_lease_is_reclaimed(persistence) -> None:
    repository, queue, sessions = persistence
    task = _task()
    repository.create_task(task)
    job_id = queue.enqueue(task_id=task.id)
    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(
            lambda worker: queue.claim(worker_id=worker, lease_seconds=5),
            ["worker-a", "worker-b"],
        ))
    assert sum(item is not None for item in claims) == 1
    first = next(item for item in claims if item is not None)
    assert first.id == job_id
    with sessions.begin() as session:
        row = session.get(WorkerJobOrm, job_id)
        row.lease_until = utcnow() - timedelta(seconds=1)
    reclaimed = queue.claim(worker_id="worker-b", lease_seconds=5)
    assert reclaimed is not None and reclaimed.id == job_id
    assert reclaimed.attempts == 2


def test_failed_job_retries_then_dead_letters_and_side_effect_is_idempotent(persistence) -> None:
    repository, queue, sessions = persistence
    task = _task()
    repository.create_task(task)
    job_id = queue.enqueue(task_id=task.id)
    claimed = queue.claim(worker_id="worker")
    assert claimed
    assert queue.fail(job_id, "worker", "boom", retry=True) == "retry"
    with sessions.begin() as session:
        row = session.get(WorkerJobOrm, job_id)
        row.available_at = utcnow() - timedelta(seconds=1)
    assert queue.claim(worker_id="worker") is not None

    key, completed = repository.begin_side_effect(
        task_id=task.id, effect_type="github_comment", target="pr:7", payload={"body": "x"}
    )
    assert completed is False
    repository.complete_side_effect(key, {"comment_id": 9})
    replay_key, replay_completed = repository.begin_side_effect(
        task_id=task.id, effect_type="github_comment", target="pr:7", payload={"body": "x"}
    )
    assert replay_key == key and replay_completed is True
    with sessions() as session:
        assert session.scalar(select(SideEffectOrm).where(
            SideEffectOrm.effect_type == "github_comment"
        )) is not None


def test_retention_prevents_early_delete(persistence) -> None:
    repository, _, sessions = persistence
    task = _task()
    repository.create_task(task)
    with pytest.raises(ValueError, match="retention"):
        repository.delete_task_if_retention_elapsed(task.id)
    with sessions.begin() as session:
        row = session.get(ReviewTaskOrm, task.id)
        row.retention_until = utcnow() - timedelta(seconds=1)
    assert repository.delete_task_if_retention_elapsed(task.id)
    assert repository.get_task(task.id) is None


class _InterruptState(TypedDict, total=False):
    answer: dict


@pytest.mark.asyncio
async def test_langgraph_interrupt_resumes_after_checkpointer_reopen(tmp_path: Path) -> None:
    async def wait_for_answer(_state: _InterruptState) -> _InterruptState:
        return {"answer": interrupt({"question": "continue?"})}

    def build():
        graph = StateGraph(_InterruptState)
        graph.add_node("wait", wait_for_answer)
        graph.set_entry_point("wait")
        graph.add_edge("wait", END)
        return graph

    config = {"configurable": {"thread_id": "task-1", "checkpoint_ns": "review"}}
    db = (tmp_path / "checkpoint.db").as_posix()
    async with AsyncSqliteSaver.from_conn_string(db) as saver:
        await saver.setup()
        paused = await build().compile(checkpointer=saver).ainvoke({}, config=config)
        assert paused["__interrupt__"]
    async with AsyncSqliteSaver.from_conn_string(db) as saver:
        await saver.setup()
        resumed = await build().compile(checkpointer=saver).ainvoke(
            Command(resume={"text": "yes"}), config=config
        )
        assert resumed["answer"] == {"text": "yes"}


def test_alembic_migration_upgrades_and_rolls_back(tmp_path: Path) -> None:
    database = tmp_path / "migration.db"
    env = {**os.environ, "REPOGUARDIAN_DB_PATH": str(database)}
    backend = Path(__file__).parents[1]
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "20260801_0001"],
        cwd=backend, env=env, check=True, capture_output=True, text=True,
    )
    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute(
            "select name from sqlite_master where type='table'"
        )}
        version = connection.execute("select version_num from alembic_version").fetchone()
    assert {"review_tasks", "review_units", "human_requests", "worker_jobs"} <= tables
    assert "runner_registrations" not in tables
    assert version == ("20260801_0001",)

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=backend, env=env, check=True, capture_output=True, text=True,
    )
    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute(
            "select name from sqlite_master where type='table'"
        )}
        version = connection.execute("select version_num from alembic_version").fetchone()
    assert {
        "review_tasks",
        "review_units",
        "human_requests",
        "worker_jobs",
        "runner_registrations",
        "user_validation_requests",
        "runner_result_idempotency",
        "project_ci_requests",
        "project_ci_webhook_deliveries",
    } <= tables
    assert version == ("20260804_0002",)
    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "base"],
        cwd=backend, env=env, check=True, capture_output=True, text=True,
    )
    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute(
            "select name from sqlite_master where type='table'"
        )}
    assert "review_tasks" not in tables
