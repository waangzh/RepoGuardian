"""应用依赖组装：服务单例与生命周期入口，路由只消费这里构造的实例。"""

import asyncio
import logging
from contextlib import asynccontextmanager
from functools import lru_cache

from fastapi import FastAPI

from app.core.config import settings
from app.llm.providers import build_provider
from app.services.preview.preview_jobs import PreviewJobs
from app.services.publication.publication_repository import PublicationRepository
from app.services.publication.publication_service import PublicationService
from app.services.review.review_repository import ReviewRepository
from app.services.review.review_service import ReviewService
from app.services.review.task_queue import DatabaseTaskQueue
from app.services.review.report_service import ReportService
from app.services.workspaces.maintenance_service import MaintenanceService
from app.tools.diff_parser import DiffParser
from app.tools.git_tool import GitTool
from app.tools.github.github_publisher import GitHubPublisher
from app.tools.github.github_tool import GitHubTool


@lru_cache
def get_review_service() -> ReviewService:
    """获取全局单例 ReviewService（惰性初始化，含所有依赖注入）。"""
    return ReviewService(
        github_tool=GitHubTool(settings.github_token),
        git_tool=GitTool(settings.repoguardian_workdir, settings.repoguardian_git_bin),
        diff_parser=DiffParser(),
        provider=build_provider(
            settings.repoguardian_provider,
            settings.openai_api_key,
            settings.openai_base_url,
            settings.repoguardian_model,
        ),
        report_service=ReportService(),
        repository=ReviewRepository(),
        task_queue=DatabaseTaskQueue(),
    )


@lru_cache
def get_preview_jobs() -> PreviewJobs:
    return PreviewJobs(get_review_service())


@lru_cache
def get_publication_service() -> PublicationService:
    token = settings.repoguardian_github_publish_token
    return PublicationService(PublicationRepository(), GitHubPublisher(token.get_secret_value() if token else None),
                              settings, get_review_service().get_task)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from app.graph.checkpointer import close_checkpointer
    from app.languages.isolation import close_parser_workers

    resolved = settings.resolve_model_profile()
    log = logging.getLogger("RepoGuardian.LLM")
    (log.warning if resolved.source == "generic_fallback" else log.info)(
        "启动模型窗口配置：model=%s model_profile_source=%s profile=%s",
        settings.repoguardian_model, resolved.source, resolved.profile.model_dump(mode="json"))
    service = get_review_service()
    repository = service.repository
    assert repository is not None  # 生产组装始终注入持久化仓库
    maintenance = MaintenanceService(repository, active_workspace_paths=service.active_workspace_paths)
    await maintenance.run_once()
    maintenance_task = asyncio.create_task(maintenance.run_forever())
    service.start_worker()
    previews = get_preview_jobs()
    previews.start()
    publication = get_publication_service() if settings.repoguardian_github_publish_enabled else None
    if publication:
        publication.start()
    try:
        yield
    finally:
        await previews.close()
        if publication:
            await publication.close()
        maintenance.stop()
        await maintenance_task
        await service.close()
        await asyncio.to_thread(close_parser_workers)
        await close_checkpointer()
