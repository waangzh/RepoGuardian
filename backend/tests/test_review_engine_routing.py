import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.models.review import ReviewCreateRequest, ReviewTask, TaskStatus
from app.services.review_service import ReviewService
from test_phase6a_persistence import persistence as persistence


def test_review_engine_default_is_legacy_and_settings_can_select_v1(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "repoguardian_review_engine", "legacy_units")
    assert ReviewCreateRequest(pr_url="https://github.com/o/r/pull/1").engine == "legacy_units"
    settings = Settings(_env_file=None, repoguardian_review_engine="task_review_v1")
    assert settings.repoguardian_review_engine == "task_review_v1"


def test_v1_creation_rejects_legacy_side_effect_options():
    with pytest.raises(ValidationError, match="task_review_v1"):
        ReviewCreateRequest(pr_url="https://github.com/o/r/pull/1", engine="task_review_v1",
                            generate_patches=True)
    with pytest.raises(ValidationError, match="task_review_v1"):
        ReviewCreateRequest(pr_url="https://github.com/o/r/pull/1", engine="task_review_v1",
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
    monkeypatch.setattr(settings, "repoguardian_review_engine", "legacy_units")
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
    assert any("P1" in warning for warning in task.warnings)
    with pytest.raises(ValueError, match="legacy unit retry is forbidden"):
        await service.retry_unit(task.id, "any")
