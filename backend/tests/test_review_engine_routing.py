import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.models.review import ReviewCreateRequest, ReviewPreviewRequest, ReviewTask, TaskStatus
from app.services.review_service import ReviewService
from test_phase6a_persistence import persistence as persistence


def test_review_engine_defaults_to_task_runtime(monkeypatch):
    from app.core.config import settings

    monkeypatch.delenv("REPOGUARDIAN_REVIEW_ENGINE", raising=False)
    monkeypatch.delenv("repoguardian_review_engine", raising=False)
    defaults = Settings(_env_file=None)
    assert defaults.repoguardian_review_engine == "task_review_v1"
    monkeypatch.setattr(settings, "repoguardian_review_engine", defaults.repoguardian_review_engine)
    for request_type in (ReviewCreateRequest, ReviewPreviewRequest):
        assert request_type(pr_url="https://github.com/o/r/pull/1").engine == "task_review_v1"


@pytest.mark.parametrize("request_type", [ReviewCreateRequest, ReviewPreviewRequest])
def test_retired_legacy_engine_is_rejected_on_new_requests(request_type):
    """旧引擎已下线：显式选择必须被拒绝，而不是静默降级为默认引擎。"""
    with pytest.raises(ValidationError, match="legacy_units"):
        request_type(pr_url="https://github.com/o/r/pull/1", engine="legacy_units")


def test_retired_legacy_engine_is_rejected_in_configuration(monkeypatch):
    monkeypatch.setenv("REPOGUARDIAN_REVIEW_ENGINE", "legacy_units")
    with pytest.raises(ValidationError, match="repoguardian_review_engine"):
        Settings(_env_file=None)


def test_task_review_v1_rejects_legacy_side_effect_options():
    with pytest.raises(ValidationError, match="read-only"):
        ReviewCreateRequest(pr_url="https://github.com/o/r/pull/1",
                            generate_patches=True)
    with pytest.raises(ValidationError, match="read-only"):
        ReviewCreateRequest(pr_url="https://github.com/o/r/pull/1",
                            validation_backend="gvisor")


def test_engine_is_part_of_task_snapshot_and_old_task_defaults_to_legacy():
    task = ReviewTask.model_validate({"id": "old", "pr_url": "https://github.com/o/r/pull/1"})
    assert task.engine == "legacy_units"
    v1 = ReviewTask(id="v1", pr_url="https://github.com/o/r/pull/1", engine="task_review_v1")
    assert v1.model_dump(mode="json")["engine"] == "task_review_v1"
    with pytest.raises(ValidationError, match="frozen"):
        v1.engine = "legacy_units"


def test_persisted_engine_survives_hot_config_and_rejects_changed_snapshot(persistence, monkeypatch):
    from app.core.config import settings

    repository, _, _ = persistence
    task = ReviewTask(id="v1", pr_url="https://github.com/o/r/pull/1", engine="task_review_v1")
    repository.create_task(task)
    monkeypatch.setattr(settings, "repoguardian_review_engine", "task_review_v1")
    assert repository.get_task("v1").engine == "task_review_v1"
    with pytest.raises(ValueError, match="engine is frozen"):
        repository.save_task(task.model_copy(update={"engine": "legacy_units"}))
    assert repository.get_task("v1").engine == "task_review_v1"


@pytest.mark.asyncio
async def test_v1_service_entry_does_not_fall_back_to_legacy_graph():
    service = ReviewService(None, None, None, None, None)
    task = service.create_task(ReviewCreateRequest(
        pr_url="https://github.com/o/r/pull/1", engine="task_review_v1",
    ))
    await service._run_tasks[task.id]
    assert task.status == TaskStatus.failed
    assert task.error == "task_review_v1_production_entry_not_enabled"
    assert not task.review_unit_results
    assert any("Provider" in warning for warning in task.warnings)


@pytest.mark.asyncio
async def test_historical_legacy_task_retry_is_rejected():
    """历史 legacy 任务只读：重试必须给出明确原因，不执行旧引擎代码。"""
    service = ReviewService(None, None, None, None, None)
    task = ReviewTask(id="legacy", pr_url="https://github.com/o/r/pull/1")
    service._tasks[task.id] = task
    with pytest.raises(ValueError, match="legacy_units engine is retired"):
        await service.retry_unit(task.id, "any")
