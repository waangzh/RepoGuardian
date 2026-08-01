from typing import Any

import pytest
from pydantic import ValidationError

from app.graph.nodes.optional_validation import optional_validation_node
from app.graph.nodes.report import complete_node
from app.models.review import (
    AgentAction,
    PatchResult,
    PatchValidationRequest,
    PatchValidationResult,
    RepositorySnapshot,
    ReviewCreateRequest,
    ReviewTask,
    ValidationCapabilities,
    ValidationStatus,
)
from app.validation import (
    NoValidationBackend,
    ValidationBackendPolicyError,
    ValidationBackendRegistry,
    ValidationBackendSelector,
)
from app.services.validation_service import ValidationService


class StubBackend:
    name = "gvisor"

    def __init__(self, status: ValidationStatus, *, trusted: bool = True) -> None:
        self.status = status
        self.trusted = trusted
        self.calls = 0

    async def capabilities(self, repository: RepositorySnapshot) -> ValidationCapabilities:
        return ValidationCapabilities(
            available=True,
            supported_languages=["python"],
            supported_profiles=["test"],
            executes_untrusted_code=False,
            requires_user_configuration=False,
        )

    async def validate(self, request: PatchValidationRequest) -> PatchValidationResult:
        self.calls += 1
        return PatchValidationResult(
            backend=self.name,
            status=self.status,
            head_sha=request.head_sha,
            patch_sha=request.patch_sha,
            checks=[],
            resolved_failures=[],
            new_failures=[],
            trusted=self.trusted,
        )


def _selector(backend: Any) -> ValidationBackendSelector:
    return ValidationBackendSelector(
        ValidationBackendRegistry([NoValidationBackend(), backend])
    )


def _state(backend: Any, *, patches: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    patch = PatchResult(
        id="patch-1",
        issue_id="issue-1",
        diff_content="diff --git a/a.py b/a.py\n",
        status="unverified",
        head_sha="head",
        patch_sha="patch-sha",
    )
    return {
        "task_id": "task-1",
        "mode": "review_suggest_and_validate",
        "validation_backend": "gvisor",
        "base_sha": "base",
        "head_sha": "head",
        "patches": patches if patches is not None else [patch.model_dump(mode="json")],
        "validation_results": [],
        "warnings": [],
        "_validation_backend_selector": _selector(backend),
    }


@pytest.mark.asyncio
async def test_none_is_available_and_returns_trusted_unsupported_without_execution() -> None:
    backend = NoValidationBackend()
    capabilities = await backend.capabilities(RepositorySnapshot(language="python", total_files=1))
    result = await backend.validate(PatchValidationRequest(
        task_id="task",
        patch_id="patch",
        repository_id="owner/repo",
        base_sha="base",
        head_sha="head",
        patch_sha="patch-sha",
    ))

    assert capabilities.available is True
    assert capabilities.executes_untrusted_code is False
    assert result.status == ValidationStatus.unsupported
    assert result.trusted is True
    assert result.checks == []


def test_registry_and_selector_enforce_server_policy() -> None:
    selector = ValidationBackendSelector()
    assert selector.registry.names == ("none", "user_runner", "project_ci", "gvisor")
    assert selector.select("gvisor", "review").name == "none"
    assert selector.select("project_ci", "review_and_suggest").name == "none"
    assert selector.select("none", "review_suggest_and_validate").name == "none"
    with pytest.raises(ValidationBackendPolicyError, match="not registered"):
        selector.select("model_invented_backend", "review_suggest_and_validate")
    assert not isinstance(selector.registry.get("none"), ValidationService)


@pytest.mark.asyncio
async def test_unconfigured_backends_report_unavailability_without_local_fallback() -> None:
    selector = ValidationBackendSelector()
    backend = selector.select("project_ci", "review_suggest_and_validate")
    capabilities = await backend.capabilities(
        RepositorySnapshot(language="python", total_files=1)
    )
    assert backend.name == "project_ci"
    assert capabilities.available is False
    assert capabilities.unavailable_reason == "project CI is not configured"


def test_review_request_forces_none_backend() -> None:
    request = ReviewCreateRequest(
        pr_url="https://github.com/owner/repo/pull/1",
        mode="review",
        validation_backend="gvisor",
    )
    assert request.validation_backend.value == "none"


def test_model_action_cannot_select_validation_backend() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        AgentAction(
            action="task_done",
            reason="done",
            validation_backend="gvisor",
        )


@pytest.mark.asyncio
async def test_validation_node_does_not_call_backend_without_patch() -> None:
    backend = StubBackend(ValidationStatus.passed)
    result = await optional_validation_node(_state(backend, patches=[]))
    assert result == {}
    assert backend.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_patch_status"),
    [
        (ValidationStatus.passed, "verified"),
        (ValidationStatus.failed, "validation_failed"),
        (ValidationStatus.unsupported, "unverified"),
        (ValidationStatus.infrastructure_error, "validation_inconclusive"),
    ],
)
async def test_validation_result_mapping(
    status: ValidationStatus,
    expected_patch_status: str,
) -> None:
    backend = StubBackend(status)
    result = await optional_validation_node(_state(backend))

    assert result["patches"][0]["status"] == expected_patch_status
    assert result["validation_results"][0]["backend"] == "gvisor"
    assert result["validation_results"][0]["trusted"] is True
    if status == ValidationStatus.infrastructure_error:
        assert result["patches"][0]["status"] != "validation_failed"


@pytest.mark.asyncio
async def test_untrusted_pass_cannot_verify_patch() -> None:
    result = await optional_validation_node(
        _state(StubBackend(ValidationStatus.passed, trusted=False))
    )
    assert result["patches"][0]["status"] == "validation_inconclusive"
    assert result["validation_results"][0]["status"] == "inconclusive"


@pytest.mark.asyncio
async def test_none_unsupported_keeps_review_completed() -> None:
    state = _state(NoValidationBackend())
    state["validation_backend"] = "none"
    state["_validation_backend_selector"] = ValidationBackendSelector()
    validation = await optional_validation_node(state)
    completed = await complete_node({**state, **validation})

    assert validation["patches"][0]["status"] == "unverified"
    assert validation["warnings"] == []
    assert completed["status"] == "completed"


def test_review_api_payload_preserves_backend_and_trusted() -> None:
    backend = StubBackend(ValidationStatus.passed)
    result = PatchValidationResult(
        backend=backend.name,
        status=ValidationStatus.passed,
        head_sha="head",
        patch_sha="patch-sha",
        checks=[],
        resolved_failures=[],
        new_failures=[],
        trusted=True,
    )
    task = ReviewTask(
        id="task",
        pr_url="https://github.com/owner/repo/pull/1",
        validation=[{**result.model_dump(mode="json"), "patch_id": "patch"}],
    )
    payload = task.model_dump(mode="json")
    assert payload["validation"][0]["backend"] == "gvisor"
    assert payload["validation"][0]["trusted"] is True
