"""旧引擎下线后的历史数据只读契约与拒绝路径。"""

import pytest

from app.models.review import (
    ReviewCoverage,
    ReviewRunManifest,
    ReviewTask,
    TaskStatus,
)
from app.services.report_service import ReportService
from app.services.review_service import ReviewService
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


def test_legacy_coverage_version_is_still_accepted():
    coverage = ReviewCoverage.model_validate({"coverage_version": "legacy-units-v1"})
    assert coverage.coverage_version == "legacy-units-v1"


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
async def test_legacy_task_retry_is_refused_with_explicit_reason():
    """旧任务只读：重试必须显式拒绝，不能执行已删除的旧引擎代码。"""
    service = ReviewService(None, None, None, None, None)
    service._tasks["legacy-task"] = ReviewTask.model_validate(_legacy_snapshot())

    with pytest.raises(ValueError, match="legacy_units engine is retired"):
        await service.retry_unit("legacy-task", "unit-1")


@pytest.mark.asyncio
async def test_legacy_task_retry_refusal_precedes_runtime_report_check():
    """拒绝路径不能依赖新引擎的运行报告，否则会给出误导性错误。"""
    service = ReviewService(None, None, None, None, None)
    service._tasks["legacy-task"] = ReviewTask.model_validate(_legacy_snapshot())
    assert service.get_task("legacy-task").task_runtime_report is None

    with pytest.raises(ValueError, match="historical tasks are read-only"):
        await service.retry_unit("legacy-task", "any-unit")


def test_new_tasks_cannot_choose_the_retired_engine():
    from pydantic import ValidationError

    from app.models.review import ReviewCreateRequest, ReviewPreviewRequest

    for request_type in (ReviewCreateRequest, ReviewPreviewRequest):
        with pytest.raises(ValidationError):
            request_type(pr_url="https://github.com/o/r/pull/1", engine="legacy_units")
        assert request_type(pr_url="https://github.com/o/r/pull/1").engine == "task_review_v1"


def test_production_graph_exposes_no_legacy_entry_point():
    """引擎选择已收敛：图构造不接受 phase，旧图无法被选回。"""
    from app.graph.review_graph import build_review_graph

    with pytest.raises(TypeError):
        build_review_graph(phase=2)
