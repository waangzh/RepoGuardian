"""Review routes grouped by lifecycle, source viewing and publication."""

from fastapi import APIRouter

from app.bootstrap.dependencies import get_review_service
from app.services.review.evidence.file_context import FileContextError, load_file_context
from app.tools.github.github_tool import GitHubTool, GitHubToolError
from app.api.reviews.events import router as events_router
from app.api.reviews.files import router as files_router
from app.api.reviews.findings import router as findings_router
from app.api.reviews.preview import router as preview_router
from app.api.reviews.report import router as report_router
from app.api.reviews.tasks import router as tasks_router

__all__ = ["router", "get_review_service", "FileContextError", "load_file_context",
           "GitHubTool", "GitHubToolError"]

router = APIRouter()
for child in (preview_router, tasks_router, findings_router, files_router, report_router, events_router):
    router.include_router(child)
