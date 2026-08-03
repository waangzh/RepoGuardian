import hashlib
import hmac
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.project_ci import ProjectCIWorkflow, ProjectCIWorkflowRun
from app.models.review import PatchValidationRequest, ValidationCheck, ValidationStatus
from app.models.runner import RunnerRegistrationRequest, RunnerResultSubmission
from app.services.external_validation_repository import ExternalValidationRepository
from app.services.project_ci_service import ProjectCIService
from app.services.user_runner_service import UserRunnerService
from app.tools.patch_tool import normalized_patch_sha


API_TOKEN = "api-token-" + "a" * 32
HMAC_SECRET = "hmac-secret-" + "b" * 32
ADMIN_SECRET = "admin-secret-" + "c" * 32
PATCH = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a=1\n+a=2\n"


@pytest.fixture
def repository(tmp_path: Path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'external.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _foreign_keys(connection, _record):  # type: ignore[no-untyped-def]
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    yield ExternalValidationRepository(sessions)
    engine.dispose()


def _validation_request() -> PatchValidationRequest:
    return PatchValidationRequest(
        task_id="task-1",
        patch_id="patch-1",
        repository_id="owner/repo",
        repository_clone_url="https://github.com/owner/repo.git",
        repository_fetch_ref="feature",
        base_sha="base-sha",
        head_sha="head-sha",
        patch_sha=normalized_patch_sha(PATCH),
        patch_content=PATCH,
        validation_profile="unit",
    )


def _runner_service(repository: ExternalValidationRepository) -> UserRunnerService:
    return UserRunnerService(
        {"unit": "project_unit_tests"},
        repository=repository,
        credential_secret=ADMIN_SECRET,
    )


def _submission(request_id: str) -> RunnerResultSubmission:
    submission = RunnerResultSubmission(
        request_id=request_id,
        runner_id="runner-1",
        head_sha="head-sha",
        patch_sha=normalized_patch_sha(PATCH),
        profile="unit",
        checks=[ValidationCheck(name="unit", status=ValidationStatus.passed)],
        exit_status=0,
        duration_ms=100,
        environment_fingerprint="python=3.12",
        submitted_at=datetime.now(timezone.utc),
        idempotency_key="result-key-0001",
        signature="0" * 64,
    )
    signature = hmac.new(
        HMAC_SECRET.encode(), submission.canonical_payload(), hashlib.sha256
    ).hexdigest()
    return submission.model_copy(update={"signature": signature})


def test_user_runner_request_claim_and_result_survive_restart(repository) -> None:
    first = _runner_service(repository)
    first.register(RunnerRegistrationRequest(
        runner_id="runner-1",
        display_name="Runner 1",
        allowed_repositories=["owner/repo"],
        allowed_profiles=["unit"],
        api_token=API_TOKEN,
        hmac_secret=HMAC_SECRET,
    ))
    request = first.create_request(_validation_request())
    first.claim(request.request_id, API_TOKEN)

    restarted = _runner_service(repository)
    assert restarted.registered_runner_count == 1
    assert restarted.get_summary(request.request_id).status.value == "claimed"
    submission = _submission(request.request_id)
    first_receipt = restarted.submit_result(submission, API_TOKEN).receipt

    restarted_again = _runner_service(repository)
    replay = restarted_again.submit_result(submission, API_TOKEN).receipt
    assert replay.result.id == first_receipt.result.id
    assert replay.idempotent_replay is True


class FakeGitHubClient:
    async def get_workflow(self, repository: str, workflow: str) -> ProjectCIWorkflow:
        return ProjectCIWorkflow(id=77, name="RepoGuardian Validation", path=workflow)

    async def dispatch_workflow(
        self, repository: str, workflow: str, ref: str, inputs: dict[str, str]
    ) -> int:
        return 101

    async def find_workflow_run(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    async def get_workflow_run(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("not expected")

    async def get_result_artifact(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("not expected")

    async def cancel_workflow_run(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None


def _project_service(repository: ExternalValidationRepository) -> ProjectCIService:
    return ProjectCIService(
        FakeGitHubClient(),
        workflow="repoguardian-validation.yml",
        workflow_name="RepoGuardian Validation",
        ref="main",
        profiles={"unit": "unit"},
        auto_poll=False,
        repository=repository,
    )


@pytest.mark.asyncio
async def test_project_ci_run_binding_and_delivery_survive_restart(repository) -> None:
    first = _project_service(repository)
    dispatched = await first.dispatch(_validation_request())

    restarted = _project_service(repository)
    summary = restarted.get_summary(dispatched.validation_request_id)
    assert summary.run_id == 101
    run = ProjectCIWorkflowRun(
        id=101,
        repository="owner/repo",
        workflow_id=77,
        workflow_name="RepoGuardian Validation",
        ref="main",
        status="in_progress",
        display_title=f"RepoGuardian Validation {summary.request_id}",
    )
    await restarted.handle_workflow_run(
        summary.request_id, run, delivery_id="delivery-1"
    )

    restarted_again = _project_service(repository)
    replay = await restarted_again.handle_workflow_run(
        summary.request_id, run, delivery_id="delivery-1"
    )
    assert replay.idempotent_replay is True
    assert replay.status.value == "running"
