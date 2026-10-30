"""审查 API 路由 —— 任务创建、查询、SSE 进度推送。

端点：
    POST   /api/reviews             创建审查任务（异步启动图执行）
    GET    /api/reviews/{task_id}   获取任务完整状态
    GET    /api/reviews/{task_id}/report  获取 Markdown 报告
    GET    /api/reviews/{task_id}/stream   SSE 实时进度流
"""

import asyncio
import httpx

from fastapi import APIRouter, HTTPException

from app.api import reviews as _reviews
from app.core.config import settings
from app.review.domain import TERMINAL
from app.models.publication import PublicationPlan, ReviewCommentCheck, ReviewCommentOptions, ReviewSummary
from app.services.publication.publication_planner import PublicationPlanner, summary_projection

router = APIRouter(prefix="/reviews", tags=["reviews"])


async def _build_plan(task_id: str) -> PublicationPlan:
    service = _reviews.get_review_service()
    task = await asyncio.to_thread(service.get_task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status not in TERMINAL:
        raise HTTPException(status_code=409, detail="Review is not finished")
    dispositions = await _report_dispositions(service, task_id)
    return await asyncio.to_thread(PublicationPlanner(service._artifacts).build,
                                   task.model_copy(deep=True), dispositions=dispositions)


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
    return await asyncio.to_thread(service.finding_dispositions, task_id)


def _comment_content(plan, options):
    from app.services.publication.publication_renderer import render_review_comment, render_finding_comment, comment_selection

    if options.finding_key is not None:
        findings = [*plan.inline_drafts, *plan.summary_findings]
        selected = next((item for item in findings if item.finding_key == options.finding_key), None)
        if selected is None:
            raise HTTPException(status_code=409, detail="Selected finding is no longer eligible for a comment")
        return [selected], [item for item in findings if item.finding_key != selected.finding_key], [], render_finding_comment(plan, selected)
    included, omitted, retained = comment_selection(plan, exclude_resolved=options.exclude_resolved)
    return included, omitted, retained, render_review_comment(plan, detail=options.detail,
        exclude_resolved=options.exclude_resolved)


@router.post("/{task_id}/review-comment-check", response_model=ReviewCommentCheck)
async def check_review_comment(task_id: str, options: ReviewCommentOptions | None = None):
    """只读检查复制时的 Head；不使用发布权限、不保存发布状态。"""
    import re
    from datetime import datetime, timezone
    from app.services.publication.publication_renderer import markdown_text

    plan = await _build_plan(task_id)
    options = options or ReviewCommentOptions()
    included, omitted, retained, comment = _comment_content(plan, options)
    latest = None
    state = "unknown"
    if plan.snapshot.head_sha and re.fullmatch(r"[a-f0-9]{40,64}", plan.snapshot.head_sha):
        try:
            live = await _reviews.GitHubTool(token=settings.github_token).fetch_pr(plan.digest.pr_url or "")
            if re.fullmatch(r"[a-f0-9]{40,64}", live.head.sha):
                latest = live.head.sha
                state = "current" if latest == plan.snapshot.head_sha else "stale"
        except (_reviews.GitHubToolError, ValueError, httpx.HTTPError, TimeoutError):
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
        latest_head_sha=latest, checked_at=checked_at, review_comment_markdown=comment + note, preview_markdown=comment,
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
        review_comment_markdown=comment, preview_markdown=comment,
        included_finding_keys=[item.finding_key for item in included], omitted_finding_keys=[item.finding_key for item in omitted],
        retained_blocking_keys=retained)


@router.get("/{task_id}/report")
async def get_report(task_id: str):
    task = await asyncio.to_thread(_reviews.get_review_service().get_task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    plan = await asyncio.to_thread(PublicationPlanner(_reviews.get_review_service()._artifacts).build, task)
    return {"report": task.report, "markdown": plan.user_report_markdown}
