"""手动确认后的投递任务入口，HTTP 请求只校验、落库与入队。"""
import asyncio
from types import SimpleNamespace

from app.models.publication import PublicationPreflightResult
from app.services.publication.publication_worker import PublicationWorker, PublicationDelivery, compose_summary
from app.services.publication.publication_preflight import PublicationPreflight
from app.services.publication.publication_repository import PublicationConflict
from app.services.review.task_queue import DatabaseTaskQueue
from app.tools.github.github_publisher import GitHubPublishError


class PublicationService:
    def __init__(self, repository, github, config, task_loader, queue=None, artifacts=None):
        self.repo, self.config, self.task_loader = repository, config, task_loader
        self.preflight_checker = PublicationPreflight(github, config, artifacts)
        self.delivery = PublicationDelivery(repository, github, self.preflight_checker, task_loader)
        self.worker = PublicationWorker(queue or DatabaseTaskQueue(), self.delivery)
        self.worker_task = None

    def start(self):
        if self.worker_task is None or self.worker_task.done():
            self.worker_task = asyncio.create_task(self.worker.run_forever())

    async def close(self):
        self.worker.stop()
        if self.worker_task:
            await self.worker_task

    async def checked(self, review_id, keys=None):
        task = await asyncio.to_thread(self.task_loader, review_id)
        if task is None:
            raise KeyError("review_not_found")
        checked = await self.preflight_checker.check(task, keys)
        checked.update(await self.delivery.sticky.preview(checked["context"]))
        findings = [item for item in checked["plan"].inline_drafts if item.finding_key in checked["locations"]]
        existing = await self.delivery.remote_findings(checked["context"], checked["head_sha"], findings, checked["locations"])
        checked["inline"]["already_published"] = len(existing)
        checked["inline"]["ready"] -= len(existing)
        checked["inline_previews"] = [{"finding_key": item.finding_key, "body_markdown": item.body_markdown,
            "location": checked["locations"].get(item.finding_key), "action": "already_published" if item.finding_key in existing
            else "summary_only" if item.placement == "summary"
            else "summary_fallback" if item.finding_key in checked["fallback"] else "create"}
            for item in sorted([*checked["plan"].inline_drafts, *checked["plan"].summary_findings],
                key=lambda item: ({"critical": 0, "high": 1, "medium": 2, "low": 3}[item.severity],
                    item.anchor.file_path, item.anchor.start_line or 0, item.finding_key))
            if item.finding_key in checked["finding_keys"]]
        checked["summary_markdown"] = compose_summary(checked["plan"], {"finding_keys": checked["finding_keys"]}, checked["fallback"],
            [SimpleNamespace(kind="inline", state="published") for _ in existing], "Prepared")
        if len(checked["summary_markdown"].encode()) > 59_000:
            raise GitHubPublishError("publication_body_too_long")
        return checked

    async def preflight(self, review_id, keys=None):
        return PublicationPreflightResult.model_validate(await self.checked(review_id, keys))

    async def create(self, review_id, selection, key):
        if await asyncio.to_thread(self.task_loader, review_id) is None:
            raise KeyError("review_not_found")
        publisher = f"user:{self.config.repoguardian_github_publish_publisher_id}"
        existing = await asyncio.to_thread(self.repo.existing_request, review_id, selection, publisher, key)
        if existing:
            self.authorize(existing)
            return existing
        checked = await self.checked(review_id, selection.finding_keys)
        if (checked["plan_hash"] != selection.expected_plan_hash
                or checked["report_version"] != selection.expected_report_version
                or checked["head_sha"] != selection.expected_head_sha):
            raise PublicationConflict("publication_plan_changed")
        if checked["requires_partial_acknowledgement"] and not selection.acknowledge_partial_review:
            raise PublicationConflict("partial_acknowledgement_required")
        return await asyncio.to_thread(self.repo.create, checked["plan"], selection, checked["context"], key)

    def authorize(self, attempt):
        allowed = {int(value.strip()) for value in self.config.repoguardian_github_publish_allowed_repositories.split(",") if value.strip().isdigit()}
        if (attempt.repository_id not in allowed or attempt.publisher_identity != f"user:{self.config.repoguardian_github_publish_publisher_id}"):
            raise GitHubPublishError("repository_not_authorized")

    async def get(self, review_id, identity):
        if await asyncio.to_thread(self.task_loader, review_id) is None:
            raise KeyError("review_not_found")
        attempt = await asyncio.to_thread(self.repo.get, identity)
        if attempt is None or attempt.review_id != review_id:
            raise KeyError("publication_not_found")
        self.authorize(attempt)
        return attempt

    async def history(self, review_id):
        if await asyncio.to_thread(self.task_loader, review_id) is None:
            raise KeyError("review_not_found")
        attempts = await asyncio.to_thread(self.repo.list, review_id)
        for attempt in attempts:
            self.authorize(attempt)
        return attempts

    async def retry(self, review_id, identity):
        await self.get(review_id, identity)
        return await asyncio.to_thread(self.repo.retry, identity)
