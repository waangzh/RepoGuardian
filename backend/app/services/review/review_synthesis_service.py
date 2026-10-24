"""权威审查保存后的单次编辑阶段；读取、预检和复制不会调用这里。"""

import asyncio
import json
import logging

from pydantic import ValidationError

from app.models.review_synthesis import ReviewSynthesisArtifact
from app.core.fingerprints import stable_hash
from app.services.review.review_run_budget import ReviewRunBudget, activate_run_budget, run_budget_phase
from app.services.review.review_synthesis_input import SOURCE_POLICY_VERSION, build_synthesis_input
from app.services.review.review_synthesis_repository import SynthesisStore
from app.services.review.review_synthesis_validator import SynthesisReferenceError, validate_review_synthesis

logger = logging.getLogger("RepoGuardian.Synthesis")

# 旧导入路径兼容：绑定校验的实现位于 review_synthesis_binding。
bound_synthesis = __import__("app.services.review.review_synthesis_binding",
                             fromlist=["bound_synthesis"]).bound_synthesis

# 校验器的固定错误码；只接受白名单，避免把异常文本或模型数据写进工件。
_VALIDATION_CODES = {
    "synthesis_language_mismatch", "synthesis_finding_not_allowed",
    "synthesis_control_character_not_allowed", "synthesis_reference_not_allowed",
    "synthesis_unknown_purpose_has_claim", "synthesis_change_summary_missing",
}


def _failure_code(exc):
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ValueError) and str(exc) in _VALIDATION_CODES:
        return str(exc)
    if isinstance(exc, (ValueError, ValidationError)):
        return "invalid_output"
    return "provider_or_storage_error"


async def freeze_review_synthesis(task, repository, provider, *, lease=None):
    policy = (task.task_runtime_configuration or {}).get("output_policy") or {}
    if (not policy.get("presentation_v1") or task.engine != "task_review_v1"
            or task.status not in {"completed", "completed_with_warnings"} or repository is None):
        return None
    from app.services.publication.publication_planner import PublicationPlanner
    plan = PublicationPlanner().build(task, include_markdown=False)
    if not plan.content_eligible:
        return None
    facts = build_synthesis_input(task, plan)
    artifact = ReviewSynthesisArtifact(prompt_version="review-synthesis-prompt-v7",
        source_policy_version=SOURCE_POLICY_VERSION,
        review_id=task.id, head_sha=facts.head_sha,
        effective_base_sha=facts.effective_base_sha, fact_bundle_hash=stable_hash(facts.model_dump(mode="json")),
        model=(task.task_runtime_configuration or {}).get("model", task.model),
        input_truncated=facts.input_truncated, truncation_reasons=facts.truncation_reasons,
        omitted_files=facts.omitted_files, omitted_hunks=facts.omitted_hunks,
        failure_code="generation_interrupted")
    store = SynthesisStore(repository, task.id, lease=lease)
    budget = None
    try:
        claimed, previous = await asyncio.to_thread(store.claim, artifact)
        if not claimed:
            # 可能已经发送过，包括网络未知；恢复时禁止重新调用。
            return ReviewSynthesisArtifact.model_validate(previous) if previous else None
        if not policy.get("synthesis_enabled"):
            artifact.failure_code = "disabled"
        elif provider is None:
            artifact.failure_code = "provider_unavailable"
        elif any(getattr(provider, attribute, None) not in {None, configured}
                 for attribute, configured in (
                     ("_base_url", (task.task_runtime_configuration or {}).get("endpoint")),
                     ("_provider_name", (task.task_runtime_configuration or {}).get("provider")),
                 ) if configured is not None):
            artifact.failure_code = "provider_configuration_changed"
        elif not any(source.kind in {"diff_observation", "verified_observation", "test_source_observation"}
                     for source in facts.sources):
            artifact.failure_code = "insufficient_input"
        elif len(json.dumps(facts.model_dump(mode="json"), ensure_ascii=False)) > policy.get("max_input_chars", 24_000):
            artifact.failure_code = "input_budget_exhausted"
        else:
            budget = await ReviewRunBudget.load_or_create("synthesis:" + task.id,
                max_tokens=policy.get("max_tokens", 16_000), max_calls=1,
                max_cost_microusd=policy.get("max_cost_microusd"), store=store)
            if budget.max_tokens == 0 or budget.max_cost_microusd == 0:
                artifact.failure_code = "budget_exhausted"
            else:
                token = run_budget_phase.set("synthesis")
                try:
                    with activate_run_budget(budget):
                        async with asyncio.timeout(policy.get("timeout_seconds", 20)):
                            response = await provider.synthesize_review(facts, artifact.model,
                                output_tokens=policy.get("output_tokens", 1800))
                    artifact.usage = response.usage.model_dump(mode="json")
                    draft = validate_review_synthesis(facts, response.value)
                    artifact = artifact.model_copy(update={"status": "ready",
                        "synthesis": draft, "failure_code": None})
                finally:
                    run_budget_phase.reset(token)
        if budget:
            artifact.budget = budget.snapshot()
        return await asyncio.to_thread(store.finish, artifact)
    except asyncio.CancelledError:
        # claim 与发送账本已落盘；停止不补发，且不把取消包装成摘要成功。
        raise
    except Exception as exc:
        reason = _failure_code(exc)
        diagnostic = exc.diagnostic if isinstance(exc, SynthesisReferenceError) else None
        logger.warning("摘要生成失败：review=%s failure_code=%s reference_failure=%s", task.id, reason,
                       diagnostic.model_dump(mode="json", exclude_none=True) if diagnostic else None)
        artifact = artifact.model_copy(update={"status": "fallback", "synthesis": None,
            "failure_code": reason, "reference_failure": diagnostic})
        usage = getattr(exc, "usage", None)
        if usage:
            artifact.usage = usage.model_dump(mode="json")
        if budget:
            artifact.budget = budget.snapshot()
        try:
            return await asyncio.to_thread(store.finish, artifact)
        except Exception:
            # claim 中的 fallback 仍可读取；不修改主审查终态。
            logger.warning("摘要工件未完成冻结：review=%s", task.id)
            return None
