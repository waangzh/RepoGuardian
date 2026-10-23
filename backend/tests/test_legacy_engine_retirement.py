"""旧引擎下线后的历史数据只读契约与拒绝路径。"""

from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest

from app.models.review import (
    ReviewRunManifest,
    ReviewTask,
    TaskStatus,
)
from app.services.report_service import ReportService
from app.services.review_service import ReviewService
from app.models.orm import WorkerJobOrm, utcnow
from app.services.task_queue import NonRetryableJobError
from test_phase6a_persistence import persistence as persistence


def _legacy_snapshot() -> dict:
    """模拟已持久化的旧引擎任务快照：没有 engine 字段的旧行。"""
    return {
        "id": "legacy-task",
        "status": "completed_with_warnings",
        "pr_url": "https://github.com/local/sample/pull/1",
        "review": {"mode": "review", "status": "completed_with_warnings", "completed": True},
        "report_markdown": "# 历史报告\n\n旧引擎产出的报告正文。",
        "coverage": {
            "coverage_version": "legacy-units-v1",
            "changed_files": 1,
            "eligible_files": 1,
            "reviewed_files": 1,
            "coverage_rate": 1.0,
            "completed_units": 1,
            "total_units": 1,
            "unit_coverage_rate": 1.0,
        },
    }


def test_old_snapshot_without_engine_defaults_to_legacy_units():
    """缺 engine 字段的历史行必须按 legacy_units 恢复，而不是被升级为新引擎。"""
    task = ReviewTask.model_validate(_legacy_snapshot())
    assert task.engine == "legacy_units"
    assert task.coverage.coverage_version == "legacy-units-v1"


def test_legacy_run_manifest_defaults_remain_readable():
    """旧 manifest 缺 engine/coverage_version 时按 legacy 恢复，不被升级为新引擎。"""
    manifest = ReviewRunManifest.model_validate({
        "review_id": "legacy-task",
        "provider": "deepseek", "model": "deepseek-chat",
        "started_at": "2026-08-01T00:00:00+00:00",
        "completed_at": "2026-08-01T00:10:00+00:00",
        "duration_ms": 600_000,
        "coverage": {"coverage_version": "legacy-units-v1"},
    })
    assert manifest.engine == "legacy_units"
    assert manifest.coverage_version == "legacy-units-v1"


def test_legacy_report_is_rendered_from_persisted_task():
    """历史任务报告仍可生成；旧引擎专属段落不因执行器删除而丢失。"""
    task = ReviewTask.model_validate(_legacy_snapshot())
    markdown = ReportService().generate(task)

    assert markdown.startswith("# RepoGuardian 代码审查报告")
    assert "task-goals-v1" not in markdown


def test_legacy_task_is_readable_through_repository(persistence):
    repository, _, _ = persistence
    repository.create_task(ReviewTask.model_validate(_legacy_snapshot()))

    loaded = repository.get_task("legacy-task")
    assert loaded.engine == "legacy_units"
    assert loaded.status == TaskStatus.completed_with_warnings
    assert loaded.report_markdown and "旧引擎产出" in loaded.report_markdown


@pytest.mark.asyncio
async def test_legacy_task_retry_refusal_precedes_runtime_report_check():
    """拒绝路径不能依赖新引擎的运行报告，否则会给出误导性错误。"""
    service = ReviewService(None, None, None, None, None)
    service._tasks["legacy-task"] = ReviewTask.model_validate(_legacy_snapshot())
    assert service.get_task("legacy-task").task_runtime_report is None

    with pytest.raises(ValueError, match="legacy_units engine is retired; historical tasks are read-only"):
        await service.retry_unit("legacy-task", "any-unit")


def test_production_graph_exposes_no_legacy_entry_point():
    """引擎选择已收敛：图构造不接受 phase，旧图无法被选回。"""
    from app.graph.review_graph import build_review_graph

    with pytest.raises(TypeError):
        build_review_graph(phase=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("job_kind", ["review", "unit_retry"])
@pytest.mark.parametrize("job_status", ["queued", "retry", "leased"])
async def test_worker_retires_old_jobs_without_changing_historical_task(
    persistence, monkeypatch, job_kind, job_status,
):
    repository, queue, sessions = persistence
    snapshot = _legacy_snapshot()
    snapshot["status"] = {"queued": "queued", "retry": "failed", "leased": "reviewing"}[job_status]
    snapshot["review"].update(status=snapshot["status"], completed=False)
    task = ReviewTask.model_validate(snapshot)
    repository.create_task(task)
    before = repository.get_task(task.id).model_dump(mode="json")
    job_id = queue.enqueue(task_id=task.id, kind=job_kind, payload={"unit_id": "unit-1"})
    with sessions.begin() as session:
        row = session.get(WorkerJobOrm, job_id)
        row.status = job_status
        if job_status == "leased":
            row.lease_owner = "previous-worker"
            row.lease_until = utcnow() - timedelta(seconds=1)
    build = Mock(side_effect=AssertionError("retired jobs must not build a graph"))
    load_budget = AsyncMock(side_effect=AssertionError("retired jobs must not load a budget"))
    cleanup = AsyncMock()
    monkeypatch.setattr("app.services.review_service.build_review_graph", build)
    monkeypatch.setattr("app.services.review_service.ReviewRunBudget.load_or_create", load_budget)
    monkeypatch.setattr("app.services.review_service.delete_thread_checkpoints", cleanup)
    service = ReviewService(None, None, None, None, None, repository=repository, task_queue=queue)

    assert await service._worker.run_once()

    assert repository.get_task(task.id).model_dump(mode="json") == before
    with sessions() as session:
        row = session.get(WorkerJobOrm, job_id)
        assert row.status == "dead_letter"
        assert row.attempts == 1
        assert "retired" in row.last_error
        assert row.lease_owner is None and row.lease_until is None
    assert not await service._worker.run_once()
    build.assert_not_called()
    load_budget.assert_not_awaited()
    cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_legacy_execution_is_rejected_before_mutating_task():
    service = ReviewService(None, None, None, None, None)
    task = ReviewTask.model_validate(_legacy_snapshot())
    service._tasks[task.id] = task
    before = task.model_dump(mode="json")

    with pytest.raises(NonRetryableJobError, match="historical tasks are read-only"):
        await service._run_graph(task.id)

    assert task.model_dump(mode="json") == before


@pytest.mark.asyncio
@pytest.mark.parametrize("execution_error", [None, RuntimeError("temporary failure")])
async def test_current_engine_jobs_still_complete_or_retry(persistence, monkeypatch, execution_error):
    repository, queue, sessions = persistence
    task = ReviewTask(id="current-task", pr_url="https://github.com/local/sample/pull/1",
                      engine="task_review_v1")
    repository.create_task(task)
    job_id = queue.enqueue(task_id=task.id)
    service = ReviewService(None, None, None, None, None, repository=repository, task_queue=queue)
    execute = AsyncMock(side_effect=execution_error)
    monkeypatch.setattr(service, "_run_graph", execute)

    assert await service._worker.run_once()

    execute.assert_awaited_once()
    with sessions() as session:
        row = session.get(WorkerJobOrm, job_id)
        assert row.status == ("retry" if execution_error else "completed")
