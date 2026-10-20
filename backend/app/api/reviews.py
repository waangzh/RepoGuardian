"""审查 API 路由 —— 任务创建、查询、SSE 进度推送。

端点：
    POST   /api/reviews             创建审查任务（异步启动图执行）
    GET    /api/reviews/{task_id}   获取任务完整状态
    GET    /api/reviews/{task_id}/report  获取 Markdown 报告
    GET    /api/reviews/{task_id}/stream   SSE 实时进度流
"""

import asyncio
import json
import httpx
from functools import lru_cache

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from sse_starlette.sse import EventSourceResponse

from app.agents.providers import build_provider
from app.core.config import settings
from app.models.review import (
    ReviewCreateRequest,
    ReviewCreateResponse,
    ReviewPreviewRequest,
    ReviewPreviewResponse,
    ReviewTask,
    ReviewUnitResult,
    TaskStatus,
)
from app.models.review_runtime import TaskRuntimeRetryResult
from app.models.quick_preview import QuickPreviewRequest, QuickPreviewResponse
from app.tools.github_tool import GitHubToolError
from app.models.publication import PublicationPlan, ReviewCommentCheck, ReviewCommentOptions, ReviewSummary
from app.models.persistence import (
    PatchDetail,
    ReviewIssueDetail,
    ReviewTaskListResponse,
    ReviewUnitDetail,
    ValidationDetail,
)
from app.services.report_service import ReportService
from app.services.publication_planner import PublicationPlanner, summary_projection
from app.services.review_service import ReviewService
from app.services.review_repository import ReviewRepository
from app.models.preview_job import PreviewJobCreate, PreviewJobResponse
from app.services.preview_jobs import PreviewJobs
from app.services.task_queue import DatabaseTaskQueue
from app.tools.diff_parser import DiffParser
from app.tools.git_tool import GitTool
from app.tools.github_tool import GitHubTool
from app.models.review_plan import FindingDisposition, FindingsResponse, FileDiffResponse, FileContextResponse
from app.services.file_context import FileContextError, load_file_context
from app.services.review_findings import finding_views

router = APIRouter(prefix="/reviews", tags=["reviews"])


@lru_cache
def get_review_service() -> ReviewService:
    """获取全局单例 ReviewService（惰性初始化，含所有依赖注入）。"""
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
        repository=ReviewRepository(),
        task_queue=DatabaseTaskQueue(),
    )


@lru_cache
def get_preview_jobs() -> PreviewJobs:
    return PreviewJobs(get_review_service())


@router.post("/preview-jobs", response_model=PreviewJobResponse, status_code=202)
async def create_preview_job(request: PreviewJobCreate):
    jobs = get_preview_jobs()
    try:
        await asyncio.to_thread(jobs.store.create, request.id, request.request)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    jobs.start()
    return await asyncio.to_thread(jobs.store.get, request.id)


@router.get("/preview-jobs/{identity}", response_model=PreviewJobResponse)
async def get_preview_job(identity: str):
    value = await asyncio.to_thread(get_preview_jobs().store.get, identity)
    if value is None:
        raise HTTPException(status_code=404, detail="预览不存在，请重新分析 PR。")
    return value


@router.post("/preview-jobs/{identity}/cancel", response_model=PreviewJobResponse)
async def cancel_preview_job(identity: str):
    store = get_preview_jobs().store
    await asyncio.to_thread(store.cancel, identity)
    return await get_preview_job(identity)


@router.post("", response_model=ReviewCreateResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_review(request: ReviewCreateRequest) -> ReviewCreateResponse:
    """创建审查任务，后台启动 LangGraph 执行，立即返回 202。"""
    try:
        task = await get_review_service().create_confirmed_task(request)
    except GitHubToolError as exc:
        raise HTTPException(status_code=exc.api_status, detail=exc.public_detail()) from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc
    return ReviewCreateResponse(task_id=task.id, status=task.status,
        confirmed_plan=task.confirmed_plan, snapshot=task.snapshot, plan_hash=task.plan_hash)


@router.get("", response_model=ReviewTaskListResponse)
async def list_reviews(
    task_status: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    q: str | None = Query(default=None, max_length=200),
    repository: str | None = Query(default=None, max_length=512),
    pr_number: int | None = Query(default=None, ge=1),
    head_sha: str | None = Query(default=None, pattern=r"^[0-9a-f]{40}$"),
) -> ReviewTaskListResponse:
    return await asyncio.to_thread(get_review_service().list_tasks,
        status=task_status, page=page, page_size=page_size, q=q,
        repository=repository, pr_number=pr_number, head_sha=head_sha)


@router.post("/preview/quick", response_model=QuickPreviewResponse)
async def quick_preview_review(request: QuickPreviewRequest) -> QuickPreviewResponse:
    from app.services.quick_preview import quick_preview, QuickPreviewStale
    from app.tools.github_tool import parse_pr_url
    try:
        parse_pr_url(str(request.pr_url))
    except ValueError:
        raise HTTPException(status_code=422, detail="请输入合法的 GitHub PR URL。") from None
    try:
        return await quick_preview(get_review_service()._github_tool, str(request.pr_url))
    except QuickPreviewStale:
        raise HTTPException(status_code=409, detail="PR快照已变化，请重新获取概览。") from None
    except GitHubToolError as exc:
        raise HTTPException(status_code=exc.api_status, detail=exc.public_detail()) from None
    except (TimeoutError, httpx.TimeoutException):
        raise HTTPException(status_code=504, detail="获取 PR 概览超时，请重试或准备完整方案。") from None
    except Exception:
        # GitHub响应可能包含凭据、私有仓库信息或错误正文，不回显上游文本。
        raise HTTPException(status_code=502, detail="获取 PR 概览失败，请检查 GitHub 访问权限后重试。") from None


@router.post("/preview", response_model=ReviewPreviewResponse)
async def preview_review(request: ReviewPreviewRequest) -> ReviewPreviewResponse:
    """返回确定性 Review Unit 计划；不会调用 LLM、执行器或目标代码。"""
    try:
        return await get_review_service().preview(request)
    except GitHubToolError as exc:
        raise HTTPException(status_code=exc.api_status, detail=exc.public_detail()) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{task_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_review(task_id: str) -> dict[str, str]:
    service = get_review_service()
    task = service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    if task.status in {
        TaskStatus.completed,
        TaskStatus.completed_with_warnings,
        TaskStatus.failed,
        TaskStatus.cancelled,
    }:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Terminal review tasks cannot be cancelled",
        )
    graph_cancelled = service.cancel_task(task_id)
    from app.validation.project_ci import get_project_ci_service

    project_ci = get_project_ci_service()
    ci_cancelled = await project_ci.cancel_for_task(task_id) if project_ci else False
    if not graph_cancelled and not ci_cancelled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Task is not running")
    from app.validation.user_runner import get_user_runner_service

    get_user_runner_service().cancel_for_task(task_id)
    return {"task_id": task_id, "status": "cancelled"}


@router.post("/{task_id}/units/{unit_id}/retry", response_model=ReviewUnitResult | TaskRuntimeRetryResult)
async def retry_review_unit(task_id: str, unit_id: str) -> ReviewUnitResult | TaskRuntimeRetryResult:
    """只重试指定 Review Unit，不重新运行其他成功 Unit。"""
    try:
        return await get_review_service().retry_unit(task_id, unit_id)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task or unit not found")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.get("/{task_id}/units/{unit_id}", response_model=ReviewUnitDetail)
async def get_review_unit(task_id: str, unit_id: str) -> ReviewUnitDetail:
    detail = get_review_service().get_unit_detail(task_id, unit_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Review Unit not found")
    return detail


@router.get("/{task_id}/issues/{issue_id}", response_model=ReviewIssueDetail)
async def get_review_issue(task_id: str, issue_id: str) -> ReviewIssueDetail:
    detail = get_review_service().get_issue_detail(task_id, issue_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Issue not found")
    return detail


@router.get("/{task_id}/patches/{patch_id}", response_model=PatchDetail)
async def get_review_patch(task_id: str, patch_id: str) -> PatchDetail:
    detail = get_review_service().get_patch_detail(task_id, patch_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Patch not found")
    return detail


@router.get("/{task_id}/validations/{validation_id}", response_model=ValidationDetail)
async def get_review_validation(task_id: str, validation_id: str) -> ValidationDetail:
    detail = get_review_service().get_validation_detail(task_id, validation_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Validation not found")
    return detail


@router.get("/{task_id}", response_model=ReviewTask)
async def get_review(task_id: str) -> ReviewTask:
    """按 ID 查询完整任务状态（包含审查问题、patch、测试结果等）。"""
    task = get_review_service().get_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task


@router.get("/{task_id}/findings", response_model=FindingsResponse)
async def get_findings(task_id: str) -> FindingsResponse:
    service = get_review_service()
    task = await asyncio.to_thread(service.get_task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    saved = (await asyncio.to_thread(service._repository.finding_dispositions, task_id)
             if service._repository else service._dispositions.get(task_id, {}))
    return FindingsResponse(items=finding_views(task, saved))


@router.patch("/{task_id}/findings/{finding_id}/disposition", response_model=FindingsResponse)
async def update_finding_disposition(task_id: str, finding_id: str, value: FindingDisposition) -> FindingsResponse:
    from datetime import datetime, timezone
    service = get_review_service()
    task = await asyncio.to_thread(service.get_task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status not in {TaskStatus.completed, TaskStatus.completed_with_warnings, TaskStatus.failed, TaskStatus.cancelled}:
        raise HTTPException(status_code=409, detail="Review must be terminal before human disposition")
    if finding_id not in {item.id for item in finding_views(task, {})}:
        raise HTTPException(status_code=404, detail="Finding not found")
    current = value.model_dump(mode="json")
    if service._repository:
        await asyncio.to_thread(service._repository.save_finding_disposition, task_id, finding_id, {"current": current})
    else:
        service._dispositions.setdefault(task_id, {})[finding_id] = {
            "current": current, "updated_at": datetime.now(timezone.utc)}
    return await get_findings(task_id)


@router.get("/{task_id}/files/{file_id}/diff", response_model=FileDiffResponse)
async def get_file_diff(task_id: str, file_id: int, cursor: int = Query(default=0, ge=0),
                        limit: int = Query(default=20, ge=1, le=100)) -> FileDiffResponse:
    task = await asyncio.to_thread(get_review_service().get_task, task_id)
    if task is None or file_id < 0 or file_id >= len(task.changed_files):
        raise HTTPException(status_code=404, detail="Task or file not found")
    file = task.changed_files[file_id]
    end = min(len(file.hunks), cursor + limit)
    return FileDiffResponse(file_id=file_id, file_path=file.file_path, old_file_path=file.old_file_path,
        snapshot=task.snapshot, hunks=[hunk.model_dump(mode="json") for hunk in file.hunks[cursor:end]],
        metadata={"change_type": file.change_type, "is_binary": file.is_binary, "hunk_count": len(file.hunks)},
        next_cursor=end if end < len(file.hunks) else None, truncated=end < len(file.hunks))


@router.get("/{task_id}/files/{file_id}/context", response_model=FileContextResponse)
async def get_file_context(task_id: str, file_id: int) -> FileContextResponse:
    task = await asyncio.to_thread(get_review_service().get_task, task_id)
    if task is None or file_id < 0 or file_id >= len(task.changed_files):
        raise HTTPException(status_code=404, detail="Task or file not found")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False,
                                     headers=headers) as client:
            return await load_file_context(task, file_id, client)
    except FileContextError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (httpx.HTTPError, TimeoutError) as exc:
        raise HTTPException(status_code=502, detail="读取固定快照上下文失败，请稍后重试") from exc


async def _build_plan(task_id: str) -> PublicationPlan:
    service = get_review_service()
    task = await asyncio.to_thread(service.get_task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status not in {TaskStatus.completed, TaskStatus.completed_with_warnings, TaskStatus.failed, TaskStatus.cancelled}:
        raise HTTPException(status_code=409, detail="Review is not finished")
    dispositions = await _report_dispositions(service, task_id)
    return await asyncio.to_thread(PublicationPlanner().build, task.model_copy(deep=True), dispositions=dispositions)


@router.get("/{task_id}/publication-summary", response_model=ReviewSummary, responses={
    404: {"description": "Task not found"}, 409: {"description": "Review is not finished"},
})
async def get_publication_summary(task_id: str) -> ReviewSummary:
    """结果概览的轻量投影；逐条正文、目标明细与执行追踪仍由完整计划按需读取。"""
    plan = await _build_plan(task_id)
    return summary_projection(plan)


@router.get("/{task_id}/publication-plan", response_model=PublicationPlan, responses={
    404: {"description": "Task not found"}, 409: {"description": "Review is not finished"},
})
async def get_publication_plan(task_id: str) -> PublicationPlan:
    """从保存的终态快照生成草稿，不调用模型或 GitHub，不持久化发布状态。"""
    return await _build_plan(task_id)


async def _report_dispositions(service, task_id):
    repository = getattr(service, "_repository", None)
    return (await asyncio.to_thread(repository.finding_dispositions, task_id) if repository
            else getattr(service, "_dispositions", {}).get(task_id, {}))


def _comment_content(plan, options):
    from app.services.publication_renderer import render_review_comment, render_finding_comment, comment_selection

    if options.finding_key is not None:
        findings = [*plan.inline_drafts, *plan.summary_findings]
        selected = next((item for item in findings if item.finding_key == options.finding_key), None)
        if selected is None:
            raise HTTPException(status_code=409, detail="Selected finding is no longer eligible for a comment")
        return [selected], [item for item in findings if item.finding_key != selected.finding_key], [], render_finding_comment(plan, selected)
    included, omitted, retained = comment_selection(plan, exclude_resolved=options.exclude_resolved,
                                                     include_discussed=options.include_discussed)
    return included, omitted, retained, render_review_comment(plan, detail=options.detail,
        exclude_resolved=options.exclude_resolved, include_discussed=options.include_discussed)


@router.post("/{task_id}/review-comment-check", response_model=ReviewCommentCheck)
async def check_review_comment(task_id: str, options: ReviewCommentOptions | None = None):
    """只读检查复制时的 Head；不使用发布权限、不保存发布状态。"""
    import re
    from datetime import datetime, timezone
    from app.services.publication_renderer import markdown_text

    plan = await _build_plan(task_id)
    options = options or ReviewCommentOptions()
    included, omitted, retained, comment = _comment_content(plan, options)
    latest = None
    state = "unknown"
    if plan.snapshot.head_sha and re.fullmatch(r"[a-f0-9]{40,64}", plan.snapshot.head_sha):
        try:
            live = await GitHubTool(token=settings.github_token).fetch_pr(plan.digest.pr_url or "")
            if re.fullmatch(r"[a-f0-9]{40,64}", live.head.sha):
                latest = live.head.sha
                state = "current" if latest == plan.snapshot.head_sha else "stale"
        except (GitHubToolError, ValueError, httpx.HTTPError, TimeoutError):
            pass  # 未能确认保持 unknown；不导出凭据、响应正文或异常文本。
    checked_at = datetime.now(timezone.utc)
    # 正常情况下正文已含简短的 Commit 声明；只有版本过期或无法确认时才追加提示，
    # 避免把检查时间与远端 Head 写进每一次复制的评论。
    note = ""
    if state != "current":
        label = {"stale": "PR 已有新提交，当前评论基于旧 Head，建议重新审查后再发布",
                 "unknown": "未能确认远端 Head，无法判断当前评论是否已经过期"}[state]
        note = (f"\n\n> 快照检查：{label}。\n> 检查时间：{checked_at.isoformat()}；"
                f"审查 Head：{markdown_text(plan.snapshot.head_sha or '未记录', 64)}；"
                f"远端 Head：{markdown_text(latest or '未知', 64)}。此状态仅代表检查时点。")
    return ReviewCommentCheck(review_id=task_id, status=state, reviewed_head_sha=plan.snapshot.head_sha,
        latest_head_sha=latest, checked_at=checked_at, review_comment_markdown=comment + note,
        included_finding_keys=[item.finding_key for item in included], omitted_finding_keys=[item.finding_key for item in omitted],
        retained_blocking_keys=retained, detail=options.detail)


@router.post("/{task_id}/review-comment-preview", response_model=ReviewCommentCheck)
async def preview_review_comment(task_id: str, options: ReviewCommentOptions):
    """选择预览只读取本地事实；复制时仍必须重新检查远端 Head。"""
    from datetime import datetime, timezone

    plan = await _build_plan(task_id)
    included, omitted, retained, comment = _comment_content(plan, options)
    return ReviewCommentCheck(review_id=task_id, status="unknown", reviewed_head_sha=plan.snapshot.head_sha,
        checked_at=datetime.now(timezone.utc), detail=options.detail,
        review_comment_markdown=comment,
        included_finding_keys=[item.finding_key for item in included], omitted_finding_keys=[item.finding_key for item in omitted],
        retained_blocking_keys=retained)


@router.get("/{task_id}/discussions")
async def get_pr_discussions(task_id: str, page: int = Query(1, ge=1),
                             page_size: int = Query(20, ge=1, le=100),
                             discussion_id: str | None = Query(None, max_length=256),
                             comment_offset: int = Query(0, ge=0),
                             comment_limit: int = Query(20, ge=1, le=100)):
    from app.services.pr_discussions import discussion_page
    service = get_review_service()
    task = await asyncio.to_thread(service.get_task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Review not found")
    try:
        return await discussion_page(task, getattr(service, "_repository", None), page=page, page_size=page_size,
            discussion_id=discussion_id, comment_offset=comment_offset, comment_limit=comment_limit)
    except LookupError:
        raise HTTPException(status_code=404, detail="Discussion not found") from None
    except (ValueError, OSError):
        raise HTTPException(status_code=409, detail="Discussion snapshot is missing or invalid") from None


@router.get("/{task_id}/report")
async def get_report(task_id: str) -> Response:
    """获取 Markdown 格式的审查报告。"""
    task = get_review_service().get_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    if task.report_markdown is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report is not ready")
    return Response(content=task.report_markdown, media_type="text/markdown; charset=utf-8")


@router.get("/{task_id}/stream")
async def stream_review(task_id: str, request: Request) -> EventSourceResponse:
    """SSE 端点：推送步骤状态变化，任务结束时发送 done。"""
    service = get_review_service()

    async def event_generator():
        last_step_signature: tuple[tuple[object, ...], ...] = ()
        last_patch_signature: tuple[tuple[str, str], ...] = ()
        while True:
            if await request.is_disconnected():
                break
            task = service.get_task(task_id)
            if task is None:
                yield {"event": "error", "data": json.dumps({"message": "Task not found"})}
                break

            steps = [s.model_dump(mode="json") for s in task.steps]
            current_signature = tuple(
                (
                    step["name"], step["status"], step.get("message"),
                    json.dumps(step.get("progress"), sort_keys=True),
                    step.get("started_at"), step.get("updated_at"), step.get("finished_at"),
                )
                for step in steps
            )
            if current_signature != last_step_signature:
                for index, step in enumerate(steps):
                    if (
                        index < len(last_step_signature)
                        and current_signature[index] == last_step_signature[index]
                    ):
                        continue
                    yield {
                        "event": "step_progress",
                        "data": json.dumps({
                            "node": step["name"],
                            "status": step["status"],
                            "message": step.get("message", ""),
                            "progress": step.get("progress"),
                            "started_at": step.get("started_at"),
                            "updated_at": step.get("updated_at"),
                        }),
                    }
                last_step_signature = current_signature

            patch_signature = tuple((patch.id, patch.status.value) for patch in task.patches)
            if patch_signature != last_patch_signature:
                for patch in task.patches:
                    yield {
                        "event": "patch_update",
                        "data": json.dumps({
                            "id": patch.id,
                            "issue_ids": patch.issue_ids,
                            "status": patch.status,
                            "touched_files": patch.touched_files,
                            "risk": patch.risk,
                            "apply_check": patch.apply_check.model_dump(mode="json"),
                            "head_sha": patch.head_sha,
                            "stale": patch.stale,
                            "warning": (
                                patch.presentation.warning if patch.presentation else None
                            ),
                        }),
                    }
                last_patch_signature = patch_signature

            if task.status.value in {
                "completed",
                "completed_with_warnings",
                "failed",
                "cancelled",
            }:
                yield {"event": "done", "data": json.dumps({"status": task.status})}
                break

            await asyncio.sleep(1.0)

    return EventSourceResponse(event_generator())
