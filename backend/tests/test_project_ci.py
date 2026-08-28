from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models.project_ci import (
    ProjectCIArtifactResult,
    ProjectCIStatus,
    ProjectCIWorkflow,
    ProjectCIWorkflowRun,
)
from app.models.review import PatchValidationRequest, ValidationCheck, ValidationStatus
from app.services.project_ci_service import ProjectCIEventRejected, ProjectCIService
from app.tools.github_actions import (
    GitHubActionsPermissionError,
    WorkflowNotFoundError,
)
from app.tools.patch_tool import normalized_patch_sha
from app.validation.project_ci import ProjectCIBackend


PATCH = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a\n+b\n"


class FakeGitHubClient:
    def __init__(self) -> None:
        self.workflow = ProjectCIWorkflow(
            id=77,
            name="RepoGuardian Validation",
            path=".github/workflows/repoguardian-validation.yml",
            state="active",
        )
        self.dispatch_run_id: int | None = 101
        self.dispatches: list[dict] = []
        self.run: ProjectCIWorkflowRun | None = None
        self.artifact: ProjectCIArtifactResult | None = None
        self.cancelled_runs: list[int] = []
        self.workflow_error: Exception | None = None

    async def get_workflow(self, repository: str, workflow: str) -> ProjectCIWorkflow:
        if self.workflow_error:
            raise self.workflow_error
        return self.workflow

    async def dispatch_workflow(
        self, repository: str, workflow: str, ref: str, inputs: dict[str, str]
    ) -> int | None:
        self.dispatches.append({
            "repository": repository,
            "workflow": workflow,
            "ref": ref,
            "inputs": inputs,
        })
        return self.dispatch_run_id

    async def find_workflow_run(
        self,
        repository: str,
        workflow: str,
        ref: str,
        validation_request_id: str,
        dispatched_after: datetime,
    ) -> ProjectCIWorkflowRun | None:
        return self.run

    async def get_workflow_run(
        self, repository: str, run_id: int
    ) -> ProjectCIWorkflowRun:
        assert self.run is not None
        return self.run

    async def get_result_artifact(
        self, repository: str, run_id: int, validation_request_id: str
    ) -> ProjectCIArtifactResult:
        assert self.artifact is not None
        return self.artifact

    async def cancel_workflow_run(self, repository: str, run_id: int) -> None:
        self.cancelled_runs.append(run_id)


def _service(client: FakeGitHubClient, **kwargs) -> ProjectCIService:
    return ProjectCIService(
        client,
        workflow="repoguardian-validation.yml",
        workflow_name="RepoGuardian Validation",
        ref="main",
        profiles={"unit": "unit"},
        auto_poll=False,
        **kwargs,
    )


def _request(*, is_fork: bool = False) -> PatchValidationRequest:
    return PatchValidationRequest(
        task_id="task-1",
        patch_id="patch-1",
        repository_id="owner/repo",
        base_sha="base",
        head_sha="head-sha",
        patch_sha=normalized_patch_sha(PATCH),
        validation_profile="unit",
        patch_content=PATCH,
        is_fork=is_fork,
    )


def _run(request_id: str, *, conclusion: str = "success", **updates) -> ProjectCIWorkflowRun:
    values = {
        "id": 101,
        "repository": "owner/repo",
        "workflow_id": 77,
        "workflow_name": "RepoGuardian Validation",
        "ref": "main",
        "status": "completed",
        "conclusion": conclusion,
        "display_title": f"RepoGuardian Validation {request_id}",
        "html_url": "https://github.com/owner/repo/actions/runs/101",
    }
    values.update(updates)
    return ProjectCIWorkflowRun(**values)


def _artifact(request_id: str, **updates) -> ProjectCIArtifactResult:
    values = {
        "validation_request_id": request_id,
        "repository": "owner/repo",
        "workflow_name": "RepoGuardian Validation",
        "ref": "main",
        "run_id": 101,
        "head_sha": "head-sha",
        "patch_sha": normalized_patch_sha(PATCH),
        "profile": "unit",
        "checks": [ValidationCheck(name="unit", status="passed")],
    }
    values.update(updates)
    return ProjectCIArtifactResult(**values)


@pytest.mark.asyncio
async def test_workflow_missing_is_unsupported() -> None:
    client = FakeGitHubClient()
    client.workflow_error = WorkflowNotFoundError("missing")
    result = await ProjectCIBackend(_service(client)).validate(_request())
    assert result.status == ValidationStatus.unsupported
    assert client.dispatches == []


@pytest.mark.asyncio
async def test_permission_error_is_infrastructure_error() -> None:
    client = FakeGitHubClient()
    client.workflow_error = GitHubActionsPermissionError("forbidden", status_code=403)
    result = await ProjectCIBackend(_service(client)).validate(_request())
    assert result.status == ValidationStatus.infrastructure_error
    assert result.trusted is False


@pytest.mark.asyncio
async def test_dispatch_inputs_bind_request_head_and_patch_sha() -> None:
    client = FakeGitHubClient()
    service = _service(client)
    result = await ProjectCIBackend(service).validate(_request())
    inputs = client.dispatches[0]["inputs"]
    assert inputs["validation_request_id"] == result.validation_request_id
    assert inputs["head_sha"] == "head-sha"
    assert inputs["patch_sha"] == normalized_patch_sha(PATCH)
    assert inputs["profile"] == "unit"
    assert set(inputs) == {
        "validation_request_id", "head_sha", "patch_sha", "patch_artifact", "profile"
    }
    assert "command" not in inputs
    assert inputs["patch_artifact"].startswith("inline-base64:")
    assert service.get_summary(result.validation_request_id).status == ProjectCIStatus.dispatched


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"repository": "attacker/repo"}, "repository mismatch"),
        ({"id": 999}, "run ID mismatch"),
    ],
)
async def test_webhook_rejects_run_identity_mismatch(updates: dict, message: str) -> None:
    client = FakeGitHubClient()
    service = _service(client)
    dispatched = await service.dispatch(_request())
    with pytest.raises(ProjectCIEventRejected, match=message):
        await service.handle_workflow_run(
            dispatched.validation_request_id,
            _run(dispatched.validation_request_id, **updates),
        )


@pytest.mark.asyncio
async def test_webhook_rejects_check_suite_change() -> None:
    client = FakeGitHubClient()
    service = _service(client)
    dispatched = await service.dispatch(_request())
    await service.handle_workflow_run(
        dispatched.validation_request_id,
        _run(
            dispatched.validation_request_id,
            status="in_progress",
            conclusion=None,
            check_suite_id=501,
        ),
    )
    with pytest.raises(ProjectCIEventRejected, match="check suite ID mismatch"):
        await service.handle_workflow_run(
            dispatched.validation_request_id,
            _run(dispatched.validation_request_id, check_suite_id=502),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"head_sha": "other"}, "head SHA mismatch"),
        ({"patch_sha": "0" * 64}, "patch SHA mismatch"),
    ],
)
async def test_result_rejects_sha_mismatch(updates: dict, message: str) -> None:
    client = FakeGitHubClient()
    service = _service(client)
    dispatched = await service.dispatch(_request())
    client.artifact = _artifact(dispatched.validation_request_id, **updates)
    with pytest.raises(ProjectCIEventRejected, match=message):
        await service.handle_workflow_run(
            dispatched.validation_request_id, _run(dispatched.validation_request_id)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("conclusion", "expected"),
    [
        ("skipped", ProjectCIStatus.inconclusive),
        ("neutral", ProjectCIStatus.inconclusive),
        ("cancelled", ProjectCIStatus.cancelled),
    ],
)
async def test_non_success_conclusions_are_mapped(conclusion: str, expected: ProjectCIStatus) -> None:
    client = FakeGitHubClient()
    service = _service(client)
    dispatched = await service.dispatch(_request())
    receipt = await service.handle_workflow_run(
        dispatched.validation_request_id,
        _run(dispatched.validation_request_id, conclusion=conclusion),
    )
    assert receipt.status == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_kind", "expected"),
    [
        ("test", ProjectCIStatus.failed),
        ("infrastructure", ProjectCIStatus.infrastructure_error),
    ],
)
async def test_test_failure_and_infrastructure_failure_are_distinct(
    failure_kind: str, expected: ProjectCIStatus
) -> None:
    client = FakeGitHubClient()
    service = _service(client)
    dispatched = await service.dispatch(_request())
    client.artifact = _artifact(
        dispatched.validation_request_id,
        failure_kind=failure_kind,
        checks=[ValidationCheck(name="unit", status="failed")],
    )
    receipt = await service.handle_workflow_run(
        dispatched.validation_request_id,
        _run(dispatched.validation_request_id, conclusion="failure"),
    )
    assert receipt.status == expected


@pytest.mark.asyncio
async def test_duplicate_webhook_is_idempotent() -> None:
    client = FakeGitHubClient()
    service = _service(client)
    dispatched = await service.dispatch(_request())
    client.artifact = _artifact(dispatched.validation_request_id)
    run = _run(dispatched.validation_request_id)
    first = await service.handle_workflow_run(
        dispatched.validation_request_id, run, delivery_id="delivery-1"
    )
    replay = await service.handle_workflow_run(
        dispatched.validation_request_id, run, delivery_id="delivery-1"
    )
    assert first.status == ProjectCIStatus.passed
    assert replay.idempotent_replay is True


@pytest.mark.asyncio
async def test_fork_pr_is_not_dispatched_by_default() -> None:
    client = FakeGitHubClient()
    result = await _service(client).dispatch(_request(is_fork=True))
    assert result.status == ValidationStatus.unsupported
    assert "fork PR" in result.checks[0].detail
    assert client.dispatches == []


@pytest.mark.asyncio
async def test_workflow_success_without_required_profile_check_is_inconclusive() -> None:
    client = FakeGitHubClient()
    service = _service(client)
    dispatched = await service.dispatch(_request())
    client.artifact = _artifact(
        dispatched.validation_request_id,
        checks=[ValidationCheck(name="lint", status="passed")],
    )
    receipt = await service.handle_workflow_run(
        dispatched.validation_request_id, _run(dispatched.validation_request_id)
    )
    assert receipt.status == ProjectCIStatus.inconclusive
    assert service.get_result(dispatched.validation_request_id).status == ValidationStatus.inconclusive


@pytest.mark.asyncio
async def test_only_passed_required_check_produces_trusted_pass() -> None:
    client = FakeGitHubClient()
    service = _service(client)
    dispatched = await service.dispatch(_request())
    client.artifact = _artifact(dispatched.validation_request_id)
    await service.handle_workflow_run(
        dispatched.validation_request_id, _run(dispatched.validation_request_id)
    )
    result = service.get_result(dispatched.validation_request_id)
    assert result.status == ValidationStatus.passed
    assert result.trusted is True
    assert result.head_sha == "head-sha"
    assert result.patch_sha == normalized_patch_sha(PATCH)


@pytest.mark.asyncio
async def test_timeout_does_not_raise_and_attempts_workflow_cancel() -> None:
    current = datetime(2026, 1, 1, tzinfo=timezone.utc)
    client = FakeGitHubClient()
    service = _service(
        client,
        timeout=timedelta(seconds=60),
        now=lambda: current,
    )
    dispatched = await service.dispatch(_request())
    current += timedelta(seconds=61)
    summary = await service.poll(dispatched.validation_request_id)
    assert summary.status == ProjectCIStatus.timed_out
    assert client.cancelled_runs == [101]
    assert service.get_result(dispatched.validation_request_id).status == ValidationStatus.timed_out


@pytest.mark.asyncio
async def test_cleanup_never_deletes_repository_branches() -> None:
    current = datetime(2026, 1, 1, tzinfo=timezone.utc)
    client = FakeGitHubClient()
    service = _service(
        client,
        timeout=timedelta(seconds=60),
        now=lambda: current,
    )
    dispatched = await service.dispatch(_request(is_fork=True))
    current += timedelta(seconds=61)
    assert service.cleanup_expired() == 1
    assert not hasattr(client, "delete_ref")
    assert dispatched.status == ValidationStatus.unsupported
