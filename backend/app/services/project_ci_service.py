"""Project CI workflow dispatch、状态同步、绑定校验与结果映射。"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import uuid4

from app.models.project_ci import (
    ProjectCIArtifactResult,
    ProjectCIRequestSummary,
    ProjectCIStatus,
    ProjectCIWebhookReceipt,
    ProjectCIWorkflow,
    ProjectCIWorkflowRun,
)
from app.models.review import (
    PatchValidationRequest,
    PatchValidationResult,
    ValidationCheck,
    ValidationStatus,
)
from app.tools.github_actions import (
    GitHubActionsClient,
    GitHubActionsError,
    GitHubActionsPermissionError,
    WorkflowNotFoundError,
)
from app.tools.patch_tool import normalized_patch_sha


class ProjectCIError(RuntimeError):
    pass


class ProjectCIRequestNotFound(ProjectCIError):
    pass


class ProjectCIEventRejected(ProjectCIError):
    pass


@dataclass
class _Record:
    summary: ProjectCIRequestSummary
    workflow: ProjectCIWorkflow | None = None
    result: PatchValidationResult | None = None


class ProjectCIService:
    """内存状态协调器；不创建 Git ref，也不执行目标仓库代码。"""

    _TERMINAL = {
        ProjectCIStatus.passed,
        ProjectCIStatus.failed,
        ProjectCIStatus.cancelled,
        ProjectCIStatus.timed_out,
        ProjectCIStatus.inconclusive,
        ProjectCIStatus.infrastructure_error,
        ProjectCIStatus.unsupported,
    }

    def __init__(
        self,
        client: GitHubActionsClient,
        *,
        workflow: str,
        workflow_name: str,
        ref: str,
        profiles: dict[str, str],
        allow_fork: bool = False,
        timeout: timedelta = timedelta(hours=1),
        poll_interval: float = 30,
        max_patch_input_bytes: int = 48_000,
        auto_poll: bool = True,
        now: Callable[[], datetime] | None = None,
        on_result: Callable[[ProjectCIRequestSummary, PatchValidationResult], None] | None = None,
    ) -> None:
        self.client = client
        self.workflow = workflow
        self.workflow_name = workflow_name
        self.ref = ref
        self.profiles = dict(profiles)
        self.allow_fork = allow_fork
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.max_patch_input_bytes = max_patch_input_bytes
        self.auto_poll = auto_poll
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._on_result = on_result
        self._records: dict[str, _Record] = {}
        self._deliveries: set[str] = set()
        self._poll_tasks: dict[str, asyncio.Task[None]] = {}

    async def dispatch(self, request: PatchValidationRequest) -> PatchValidationResult:
        request_id = uuid4().hex
        now = self._now()
        summary = ProjectCIRequestSummary(
            request_id=request_id,
            task_id=request.task_id,
            patch_id=request.patch_id,
            repository=request.repository_id,
            workflow_name=self.workflow_name,
            ref=self.ref,
            head_sha=request.head_sha,
            patch_sha=request.patch_sha,
            profile=request.validation_profile or "",
            status=ProjectCIStatus.queued,
            created_at=now,
            updated_at=now,
            expires_at=now + self.timeout,
        )
        record = _Record(summary=summary)
        self._records[request_id] = record

        if request.is_fork and not self.allow_fork:
            return self._finish_without_run(
                record,
                ProjectCIStatus.unsupported,
                ValidationStatus.unsupported,
                "fork PR requires manual approval or UserRunner",
            )
        if not request.patch_content:
            return self._finish_without_run(
                record,
                ProjectCIStatus.infrastructure_error,
                ValidationStatus.infrastructure_error,
                "candidate patch content is unavailable",
            )
        if normalized_patch_sha(request.patch_content) != request.patch_sha:
            return self._finish_without_run(
                record,
                ProjectCIStatus.infrastructure_error,
                ValidationStatus.infrastructure_error,
                "candidate patch does not match patch_sha",
            )
        profile = request.validation_profile or ""
        if profile not in self.profiles:
            return self._finish_without_run(
                record,
                ProjectCIStatus.unsupported,
                ValidationStatus.unsupported,
                f"validation profile '{profile}' is not configured",
            )

        normalized_patch = request.patch_content.replace("\r\n", "\n").rstrip("\n") + "\n"
        encoded_patch = base64.b64encode(normalized_patch.encode("utf-8")).decode("ascii")
        patch_artifact = f"inline-base64:{encoded_patch}"
        if len(patch_artifact.encode("utf-8")) > self.max_patch_input_bytes:
            return self._finish_without_run(
                record,
                ProjectCIStatus.unsupported,
                ValidationStatus.unsupported,
                "candidate patch is too large for workflow_dispatch input",
            )

        try:
            workflow = await self.client.get_workflow(request.repository_id, self.workflow)
            if workflow.name != self.workflow_name or workflow.state != "active":
                raise WorkflowNotFoundError("configured RepoGuardian workflow is unavailable")
            record.workflow = workflow
            run_id = await self.client.dispatch_workflow(
                request.repository_id,
                self.workflow,
                self.ref,
                {
                    "validation_request_id": request_id,
                    "head_sha": request.head_sha,
                    "patch_sha": request.patch_sha,
                    "patch_artifact": patch_artifact,
                    "profile": profile,
                },
            )
        except WorkflowNotFoundError:
            return self._finish_without_run(
                record,
                ProjectCIStatus.unsupported,
                ValidationStatus.unsupported,
                "RepoGuardian Validation workflow is not installed",
            )
        except GitHubActionsPermissionError:
            return self._finish_without_run(
                record,
                ProjectCIStatus.infrastructure_error,
                ValidationStatus.infrastructure_error,
                "GitHub App lacks Actions read/write permission",
            )
        except GitHubActionsError as exc:
            return self._finish_without_run(
                record,
                ProjectCIStatus.infrastructure_error,
                ValidationStatus.infrastructure_error,
                str(exc),
            )

        record.summary = record.summary.model_copy(update={
            "status": ProjectCIStatus.dispatched,
            "run_id": run_id,
            "updated_at": self._now(),
            "detail": "workflow dispatched",
        })
        if self.auto_poll:
            self._poll_tasks[request_id] = asyncio.create_task(self._poll_until_terminal(request_id))
        return PatchValidationResult(
            backend="project_ci",
            status=ValidationStatus.unsupported,
            head_sha=request.head_sha,
            patch_sha=request.patch_sha,
            checks=[ValidationCheck(
                name="project_ci",
                status=ValidationStatus.unsupported,
                detail="workflow dispatched; result will be synchronized asynchronously",
            )],
            trusted=False,
            trust_source="project_ci_pending",
            validation_request_id=request_id,
            profile=profile,
        )

    def get_summary(self, request_id: str) -> ProjectCIRequestSummary:
        return self._get(request_id).summary.model_copy(deep=True)

    def get_result(self, request_id: str) -> PatchValidationResult | None:
        result = self._get(request_id).result
        return result.model_copy(deep=True) if result else None

    async def poll(self, request_id: str) -> ProjectCIRequestSummary:
        record = self._get(request_id)
        if record.summary.status in self._TERMINAL:
            return record.summary.model_copy(deep=True)
        if self._now() >= record.summary.expires_at:
            await self._timeout(record)
            return record.summary.model_copy(deep=True)

        try:
            run = None
            if record.summary.run_id is None:
                run = await self.client.find_workflow_run(
                    record.summary.repository,
                    self.workflow,
                    record.summary.ref,
                    record.summary.request_id,
                    record.summary.created_at,
                )
                if run is None:
                    return record.summary.model_copy(deep=True)
            else:
                run = await self.client.get_workflow_run(
                    record.summary.repository, record.summary.run_id
                )
            await self._accept_run(record, run)
        except ProjectCIEventRejected:
            raise
        except GitHubActionsError as exc:
            self._set_terminal(
                record,
                ProjectCIStatus.infrastructure_error,
                self._result(record, ValidationStatus.infrastructure_error, [], str(exc), trusted=False),
                str(exc),
            )
        return record.summary.model_copy(deep=True)

    async def handle_workflow_run(
        self,
        request_id: str,
        run: ProjectCIWorkflowRun,
        *,
        delivery_id: str | None = None,
    ) -> ProjectCIWebhookReceipt:
        replay = bool(delivery_id and delivery_id in self._deliveries)
        record = self._get(request_id)
        self._validate_run(record, run)
        if replay or record.summary.status in self._TERMINAL:
            return ProjectCIWebhookReceipt(
                request_id=request_id,
                status=record.summary.status,
                idempotent_replay=True,
            )
        await self._accept_run(record, run)
        if delivery_id:
            self._deliveries.add(delivery_id)
        return ProjectCIWebhookReceipt(request_id=request_id, status=record.summary.status)

    async def cancel(self, request_id: str) -> ProjectCIRequestSummary:
        record = self._get(request_id)
        if record.summary.status in self._TERMINAL:
            return record.summary.model_copy(deep=True)
        if record.summary.run_id is not None:
            try:
                await self.client.cancel_workflow_run(
                    record.summary.repository, record.summary.run_id
                )
            except GitHubActionsError:
                pass
        self._set_terminal(
            record,
            ProjectCIStatus.cancelled,
            self._result(record, ValidationStatus.cancelled, [], "validation cancelled"),
            "validation cancelled",
        )
        return record.summary.model_copy(deep=True)

    async def cancel_for_task(self, task_id: str) -> bool:
        pending = [
            item.summary.request_id for item in self._records.values()
            if item.summary.task_id == task_id and item.summary.status not in self._TERMINAL
        ]
        for request_id in pending:
            await self.cancel(request_id)
        return bool(pending)

    def cleanup_expired(self) -> int:
        """仅清理本服务的终态内存记录；永远不操作仓库分支。"""
        now = self._now()
        expired = [
            request_id for request_id, record in self._records.items()
            if record.summary.status in self._TERMINAL and record.summary.expires_at <= now
        ]
        for request_id in expired:
            self._records.pop(request_id, None)
            task = self._poll_tasks.pop(request_id, None)
            if task and not task.done():
                task.cancel()
        return len(expired)

    async def _accept_run(self, record: _Record, run: ProjectCIWorkflowRun) -> None:
        self._validate_run(record, run)
        record.summary = record.summary.model_copy(update={
            "run_id": run.id,
            "run_url": run.html_url,
            "check_suite_id": run.check_suite_id,
            "status": (
                ProjectCIStatus.running
                if run.status != "completed"
                else record.summary.status
            ),
            "updated_at": self._now(),
            "detail": f"workflow {run.status}",
        })
        if run.status != "completed":
            return

        if run.conclusion == "cancelled":
            self._set_terminal(
                record,
                ProjectCIStatus.cancelled,
                self._result(record, ValidationStatus.cancelled, [], "workflow cancelled"),
                "workflow cancelled",
            )
            return
        if run.conclusion == "timed_out":
            self._set_terminal(
                record,
                ProjectCIStatus.timed_out,
                self._result(record, ValidationStatus.timed_out, [], "workflow timed out"),
                "workflow timed out",
            )
            return
        if run.conclusion in {"skipped", "neutral", "stale", "action_required", None}:
            self._set_terminal(
                record,
                ProjectCIStatus.inconclusive,
                self._result(record, ValidationStatus.inconclusive, [], "workflow inconclusive"),
                "workflow inconclusive",
            )
            return

        try:
            artifact = await self.client.get_result_artifact(
                record.summary.repository, run.id, record.summary.request_id
            )
            self._validate_artifact(record, run, artifact)
        except ProjectCIEventRejected:
            raise
        except GitHubActionsError as exc:
            status = (
                ValidationStatus.inconclusive
                if run.conclusion == "success"
                else ValidationStatus.infrastructure_error
            )
            project_status = (
                ProjectCIStatus.inconclusive
                if status == ValidationStatus.inconclusive
                else ProjectCIStatus.infrastructure_error
            )
            self._set_terminal(
                record,
                project_status,
                self._result(record, status, [], str(exc), trusted=False),
                str(exc),
            )
            return

        project_status, validation_status, detail = self._map_result(record, run, artifact)
        self._set_terminal(
            record,
            project_status,
            self._result(
                record,
                validation_status,
                artifact.checks,
                detail,
                log_summary=artifact.log_summary,
                artifact_references=artifact.artifact_references,
            ),
            detail,
        )

    def _validate_run(self, record: _Record, run: ProjectCIWorkflowRun) -> None:
        expected = record.summary
        if run.repository.lower() != expected.repository.lower():
            raise ProjectCIEventRejected("webhook repository mismatch")
        if expected.run_id is not None and run.id != expected.run_id:
            raise ProjectCIEventRejected("workflow run ID mismatch")
        if (
            expected.check_suite_id is not None
            and run.check_suite_id is not None
            and run.check_suite_id != expected.check_suite_id
        ):
            raise ProjectCIEventRejected("check suite ID mismatch")
        if run.workflow_name != expected.workflow_name:
            raise ProjectCIEventRejected("workflow name mismatch")
        if run.ref != expected.ref:
            raise ProjectCIEventRejected("workflow ref mismatch")
        expected_title = f"RepoGuardian Validation {expected.request_id}"
        if run.display_title != expected_title:
            raise ProjectCIEventRejected("validation request ID mismatch")
        if record.workflow and run.workflow_id is not None:
            if str(run.workflow_id) != str(record.workflow.id):
                raise ProjectCIEventRejected("workflow ID mismatch")

    @staticmethod
    def _validate_artifact(
        record: _Record,
        run: ProjectCIWorkflowRun,
        artifact: ProjectCIArtifactResult,
    ) -> None:
        expected = record.summary
        comparisons = {
            "validation request ID": artifact.validation_request_id == expected.request_id,
            "repository": artifact.repository.lower() == expected.repository.lower(),
            "workflow name": artifact.workflow_name == expected.workflow_name,
            "workflow ref": artifact.ref == expected.ref,
            "run ID": artifact.run_id == run.id == expected.run_id,
            "head SHA": artifact.head_sha == expected.head_sha,
            "patch SHA": artifact.patch_sha == expected.patch_sha,
            "profile": artifact.profile == expected.profile,
        }
        mismatch = next((name for name, matches in comparisons.items() if not matches), None)
        if mismatch:
            raise ProjectCIEventRejected(f"{mismatch} mismatch")

    def _map_result(
        self,
        record: _Record,
        run: ProjectCIWorkflowRun,
        artifact: ProjectCIArtifactResult,
    ) -> tuple[ProjectCIStatus, ValidationStatus, str]:
        if run.conclusion == "failure":
            if artifact.failure_kind == "infrastructure":
                return (
                    ProjectCIStatus.infrastructure_error,
                    ValidationStatus.infrastructure_error,
                    "workflow infrastructure failure",
                )
            return ProjectCIStatus.failed, ValidationStatus.failed, "project checks failed"
        if run.conclusion != "success":
            return ProjectCIStatus.inconclusive, ValidationStatus.inconclusive, "workflow inconclusive"

        required_check = self.profiles[record.summary.profile]
        matching = [check for check in artifact.checks if check.name == required_check]
        if any(check.status == ValidationStatus.passed for check in matching):
            return ProjectCIStatus.passed, ValidationStatus.passed, "required profile check passed"
        if any(check.status == ValidationStatus.failed for check in matching):
            return ProjectCIStatus.failed, ValidationStatus.failed, "required profile check failed"
        return (
            ProjectCIStatus.inconclusive,
            ValidationStatus.inconclusive,
            "required profile check did not pass",
        )

    async def _timeout(self, record: _Record) -> None:
        if record.summary.run_id is not None:
            try:
                await self.client.cancel_workflow_run(
                    record.summary.repository, record.summary.run_id
                )
            except GitHubActionsError:
                pass
        self._set_terminal(
            record,
            ProjectCIStatus.timed_out,
            self._result(record, ValidationStatus.timed_out, [], "validation timed out"),
            "validation timed out; review result remains available",
        )

    async def _poll_until_terminal(self, request_id: str) -> None:
        try:
            while self._get(request_id).summary.status not in self._TERMINAL:
                await asyncio.sleep(self.poll_interval)
                await self.poll(request_id)
        except (asyncio.CancelledError, ProjectCIEventRejected, ProjectCIRequestNotFound):
            return
        finally:
            self._poll_tasks.pop(request_id, None)

    def _finish_without_run(
        self,
        record: _Record,
        project_status: ProjectCIStatus,
        validation_status: ValidationStatus,
        detail: str,
    ) -> PatchValidationResult:
        result = self._result(
            record,
            validation_status,
            [ValidationCheck(name="project_ci", status=validation_status, detail=detail)],
            detail,
            trusted=validation_status == ValidationStatus.unsupported,
        )
        self._set_terminal(record, project_status, result, detail)
        return result

    def _result(
        self,
        record: _Record,
        status: ValidationStatus,
        checks: list[ValidationCheck],
        detail: str,
        *,
        trusted: bool = True,
        log_summary: str | None = None,
        artifact_references: list[str] | None = None,
    ) -> PatchValidationResult:
        summary = record.summary
        return PatchValidationResult(
            backend="project_ci",
            status=status,
            head_sha=summary.head_sha,
            patch_sha=summary.patch_sha,
            checks=checks,
            trusted=trusted,
            trust_source=(
                f"project_ci:{summary.workflow_name}:{summary.run_id}"
                if summary.run_id is not None
                else "project_ci"
            ),
            validation_request_id=summary.request_id,
            profile=summary.profile,
            log_summary=log_summary or detail,
            artifact_references=artifact_references or [],
            completed_at=self._now() if status != ValidationStatus.unsupported else None,
        )

    def _set_terminal(
        self,
        record: _Record,
        status: ProjectCIStatus,
        result: PatchValidationResult,
        detail: str,
    ) -> None:
        record.summary = record.summary.model_copy(update={
            "status": status,
            "detail": detail,
            "updated_at": self._now(),
        })
        record.result = result
        if (
            self._on_result
            and record.summary.run_id is not None
            and (result.trust_source or "").startswith("project_ci")
        ):
            try:
                self._on_result(record.summary.model_copy(deep=True), result.model_copy(deep=True))
            except (KeyError, ValueError):
                # Review 图可能尚未把候选补丁同步到聚合根；webhook/查询路径会再次回写。
                pass
        task = self._poll_tasks.get(record.summary.request_id)
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def _get(self, request_id: str) -> _Record:
        try:
            return self._records[request_id]
        except KeyError as exc:
            raise ProjectCIRequestNotFound(request_id) from exc
