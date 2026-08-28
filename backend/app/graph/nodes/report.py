import logging
from datetime import datetime, timezone

from app.agents.providers import LLMProviderError, build_provider
from app.core.config import settings
from app.graph.nodes._events import append_step
from app.graph.state import ReviewState
from app.models.review import ReviewPhase
from app.services.review_rebuild import rebuild_task_from_state
from app.services.report_service import ReportService
from app.services.model_usage import append_usage, unpack_model_call
from app.services.review_manifest import build_review_manifest

logger = logging.getLogger("RepoGuardian.Node")


async def report_node(state: ReviewState) -> ReviewState:
    """报告节点：将最终图状态重建为 Pydantic 模型并生成 Markdown 报告。

    这是审查流程的终点，之后图进入 END 状态。
    """
    logger.info("📝 [报告] 开始从状态重建 ReviewTask 并生成报告...")
    final_status = _final_status(state)
    final_phase = ReviewPhase.failed if final_status == "failed" else ReviewPhase.completed
    updated_at = datetime.now(timezone.utc).isoformat()
    report_state = ReviewState(**{
        **state,
        "status": final_status,
        "phase": final_phase,
        "updated_at": updated_at,
    })
    task = rebuild_task_from_state(report_state)
    purpose_summary = None
    model_usages = append_usage(state.get("model_usages") or [], None)
    if task.pr:
        provider = state.get("_provider")
        if provider is None and state.get("_report_purpose_model_enabled"):
            provider = build_provider(
                settings.repoguardian_provider,
                settings.openai_api_key,
                settings.openai_base_url,
                settings.repoguardian_model,
            )
        if provider is not None:
            try:
                raw_result = await provider.summarize_pr_purpose(
                    task.pr, task.changed_files, task.model
                )
                purpose_summary, usage = unpack_model_call(raw_result)
                model_usages = append_usage(model_usages, usage)
            except LLMProviderError as exc:
                model_usages = append_usage(model_usages, exc.usage)
                logger.warning("PR 作用中文概括生成失败，使用确定性中文兜底：%s", exc)
    completed_at = datetime.fromisoformat(updated_at)
    manifest_state = {
        **report_state,
        "model_usages": model_usages,
    }
    manifest = build_review_manifest(manifest_state, completed_at)
    report_state = ReviewState(**{
        **manifest_state,
        "review_coverage": manifest.coverage.model_dump(mode="json"),
        "run_manifest": manifest.model_dump(mode="json"),
    })
    task = rebuild_task_from_state(report_state)
    markdown = ReportService().generate(task, purpose_summary=purpose_summary)
    logger.info("📝 [报告] 报告生成完成（%d 字符 Markdown）", len(markdown))
    return ReviewState(
        report_markdown=markdown,
        status=final_status,
        phase=final_phase,
        updated_at=updated_at,
        model_usages=model_usages,
        review_coverage=manifest.coverage.model_dump(mode="json"),
        run_manifest=manifest.model_dump(mode="json"),
        step_progress=append_step(state, "report", "completed", "报告已生成"),
    )


async def complete_node(state: ReviewState) -> ReviewState:
    """报告发布完成后才将任务标记为 completed。"""
    final_status = _final_status(state)
    if final_status == "failed":
        return ReviewState(status=final_status, phase=ReviewPhase.failed)
    return ReviewState(
        status=final_status,
        phase=ReviewPhase.completed,
    )


def _final_status(state: ReviewState) -> str:
    if state.get("status") == "failed":
        return "failed"
    return "completed_with_warnings" if state.get("warnings") else "completed"
