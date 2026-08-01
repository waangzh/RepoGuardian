"""GitHub Actions workflow dispatch 与结果 artifact 的最小 REST 客户端。"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime
from typing import Protocol, runtime_checkable

import httpx
from pydantic import ValidationError

from app.models.project_ci import (
    ProjectCIArtifactResult,
    ProjectCIWorkflow,
    ProjectCIWorkflowRun,
)


class GitHubActionsError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class WorkflowNotFoundError(GitHubActionsError):
    pass


class GitHubActionsPermissionError(GitHubActionsError):
    pass


@runtime_checkable
class GitHubActionsClient(Protocol):
    async def get_workflow(
        self, repository: str, workflow: str
    ) -> ProjectCIWorkflow: ...

    async def dispatch_workflow(
        self,
        repository: str,
        workflow: str,
        ref: str,
        inputs: dict[str, str],
    ) -> int | None: ...

    async def find_workflow_run(
        self,
        repository: str,
        workflow: str,
        ref: str,
        validation_request_id: str,
        dispatched_after: datetime,
    ) -> ProjectCIWorkflowRun | None: ...

    async def get_workflow_run(
        self, repository: str, run_id: int
    ) -> ProjectCIWorkflowRun: ...

    async def get_result_artifact(
        self, repository: str, run_id: int, validation_request_id: str
    ) -> ProjectCIArtifactResult: ...

    async def cancel_workflow_run(self, repository: str, run_id: int) -> None: ...


class HttpGitHubActionsClient:
    """仅使用 Actions read/write；不会创建、推送或删除 Git ref。"""

    def __init__(self, token: str, *, api_url: str = "https://api.github.com") -> None:
        self._token = token
        self._api_url = api_url.rstrip("/")

    def _headers(self, *, accept: str = "application/vnd.github+json") -> dict[str, str]:
        return {
            "Accept": accept,
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.request(
                method, f"{self._api_url}{path}", headers=self._headers(), **kwargs
            )
        if response.status_code == 404:
            raise WorkflowNotFoundError("GitHub workflow or run was not found", status_code=404)
        if response.status_code in {401, 403}:
            raise GitHubActionsPermissionError(
                "GitHub Actions permission is unavailable", status_code=response.status_code
            )
        if response.status_code >= 400:
            raise GitHubActionsError(
                f"GitHub Actions API failed: {response.status_code} {response.text[:300]}",
                status_code=response.status_code,
            )
        return response

    async def get_workflow(self, repository: str, workflow: str) -> ProjectCIWorkflow:
        response = await self._request(
            "GET", f"/repos/{repository}/actions/workflows/{workflow}"
        )
        return ProjectCIWorkflow.model_validate(response.json())

    async def dispatch_workflow(
        self,
        repository: str,
        workflow: str,
        ref: str,
        inputs: dict[str, str],
    ) -> int | None:
        await self._request(
            "POST",
            f"/repos/{repository}/actions/workflows/{workflow}/dispatches",
            json={"ref": ref, "inputs": inputs},
        )
        # GitHub 的 workflow_dispatch API 返回 204，不返回 run ID；由 webhook/查询绑定。
        return None

    async def find_workflow_run(
        self,
        repository: str,
        workflow: str,
        ref: str,
        validation_request_id: str,
        dispatched_after: datetime,
    ) -> ProjectCIWorkflowRun | None:
        response = await self._request(
            "GET",
            f"/repos/{repository}/actions/workflows/{workflow}/runs",
            params={"branch": ref, "event": "workflow_dispatch", "per_page": 30},
        )
        expected_title = f"RepoGuardian Validation {validation_request_id}"
        for payload in response.json().get("workflow_runs", []):
            created_at = datetime.fromisoformat(payload["created_at"].replace("Z", "+00:00"))
            if payload.get("display_title") != expected_title or created_at < dispatched_after:
                continue
            return self._parse_run(repository, payload)
        return None

    async def get_workflow_run(
        self, repository: str, run_id: int
    ) -> ProjectCIWorkflowRun:
        response = await self._request("GET", f"/repos/{repository}/actions/runs/{run_id}")
        return self._parse_run(repository, response.json())

    async def get_result_artifact(
        self, repository: str, run_id: int, validation_request_id: str
    ) -> ProjectCIArtifactResult:
        response = await self._request(
            "GET", f"/repos/{repository}/actions/runs/{run_id}/artifacts"
        )
        expected = f"repoguardian-validation-{validation_request_id}"
        artifact = next(
            (
                item for item in response.json().get("artifacts", [])
                if item.get("name") == expected and not item.get("expired", False)
            ),
            None,
        )
        if artifact is None:
            raise GitHubActionsError("structured validation result artifact is missing")
        archive = await self._request(
            "GET", f"/repos/{repository}/actions/artifacts/{artifact['id']}/zip"
        )
        try:
            with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
                payload = json.loads(bundle.read("result.json"))
        except (KeyError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
            raise GitHubActionsError("invalid structured validation result artifact") from exc
        try:
            return ProjectCIArtifactResult.model_validate(payload)
        except ValidationError as exc:
            raise GitHubActionsError("invalid structured validation result schema") from exc

    async def cancel_workflow_run(self, repository: str, run_id: int) -> None:
        await self._request("POST", f"/repos/{repository}/actions/runs/{run_id}/cancel")

    @staticmethod
    def _parse_run(repository: str, payload: dict[str, object]) -> ProjectCIWorkflowRun:
        repo_payload = payload.get("repository")
        full_name = (
            repo_payload.get("full_name")
            if isinstance(repo_payload, dict)
            else repository
        )
        return ProjectCIWorkflowRun(
            id=payload["id"],
            repository=str(full_name),
            workflow_id=payload.get("workflow_id"),
            workflow_name=str(payload.get("name") or ""),
            ref=str(payload.get("head_branch") or ""),
            status=str(payload.get("status") or ""),
            conclusion=payload.get("conclusion"),
            check_suite_id=payload.get("check_suite_id"),
            display_title=payload.get("display_title"),
            html_url=payload.get("html_url"),
        )
