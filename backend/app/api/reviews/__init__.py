"""审查 API：按端点职责拆分的子路由，统一挂载为 reviews 路由。

子路由通过本包属性访问依赖，保持既有打桩路径（如 ``reviews.get_review_service``）。
"""

from fastapi import APIRouter

from app.bootstrap.dependencies import get_preview_jobs, get_review_service
from app.services.review.file_context import FileContextError, load_file_context
from app.tools.github.github_tool import GitHubTool, GitHubToolError

# 端点函数保持包级导出，便于按原路径调用与打桩。
from app.api.reviews.events import get_pr_discussions, stream_review  # noqa: E402
from app.api.reviews.files import get_file_context, get_file_diff  # noqa: E402
from app.api.reviews.findings import get_findings, update_finding_disposition  # noqa: E402
from app.api.reviews.preview import (  # noqa: E402
    cancel_preview_job, create_preview_job, get_preview_job, preview_review, quick_preview_review,
)
from app.api.reviews.report import (  # noqa: E402
    check_review_comment, get_publication_plan, get_publication_summary, get_report, preview_review_comment,
)
from app.api.reviews.tasks import (  # noqa: E402
    cancel_review, create_review, delete_review, get_review, get_review_issue, get_review_patch,
    get_review_unit, get_review_validation, list_reviews, resume_review, retry_review_unit,
)

__all__ = [
    "router", "get_preview_jobs", "get_review_service", "GitHubTool", "GitHubToolError",
    "FileContextError", "load_file_context",
    "get_pr_discussions", "stream_review", "get_file_context", "get_file_diff",
    "get_findings", "update_finding_disposition", "cancel_preview_job", "create_preview_job",
    "get_preview_job", "preview_review", "quick_preview_review", "check_review_comment",
    "get_publication_plan", "get_publication_summary", "get_report", "preview_review_comment",
    "cancel_review", "create_review", "delete_review", "get_review", "get_review_issue",
    "get_review_patch", "get_review_unit", "get_review_validation", "list_reviews",
    "resume_review", "retry_review_unit",
]

from app.api.reviews.events import router as events_router
from app.api.reviews.files import router as files_router
from app.api.reviews.findings import router as findings_router
from app.api.reviews.preview import router as preview_router
from app.api.reviews.report import router as report_router
from app.api.reviews.tasks import router as tasks_router

router = APIRouter()
for _child in (preview_router, tasks_router, findings_router, files_router, report_router, events_router):
    router.include_router(_child)
