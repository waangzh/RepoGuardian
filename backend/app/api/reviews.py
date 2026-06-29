from functools import lru_cache

from fastapi import APIRouter, HTTPException, Response, status

from app.agents.providers import build_provider
from app.core.config import settings
from app.models.review import ReviewCreateRequest, ReviewCreateResponse, ReviewTask
from app.services.report_service import ReportService
from app.services.review_service import ReviewService
from app.tools.diff_parser import DiffParser
from app.tools.git_tool import GitTool
from app.tools.github_tool import GitHubTool

router = APIRouter(prefix="/reviews", tags=["reviews"])


@lru_cache
def get_review_service() -> ReviewService:
    provider = build_provider(
        settings.repoguardian_provider,
        settings.openai_api_key,
        settings.openai_base_url,
        settings.repoguardian_model,
    )
    return ReviewService(
        github_tool=GitHubTool(settings.github_token),
        git_tool=GitTool(settings.repoguardian_workdir, settings.repoguardian_git_bin),
        diff_parser=DiffParser(),
        provider=provider,
        report_service=ReportService(),
    )


@router.post("", response_model=ReviewCreateResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_review(request: ReviewCreateRequest) -> ReviewCreateResponse:
    task = get_review_service().create_task(request)
    return ReviewCreateResponse(task_id=task.id, status=task.status)


@router.get("/{task_id}", response_model=ReviewTask)
async def get_review(task_id: str) -> ReviewTask:
    task = get_review_service().get_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task


@router.get("/{task_id}/report")
async def get_report(task_id: str) -> Response:
    task = get_review_service().get_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    if task.report_markdown is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report is not ready")
    return Response(content=task.report_markdown, media_type="text/markdown; charset=utf-8")