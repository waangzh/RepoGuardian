import logging
from typing import Any

from app.agents.providers import build_provider
from app.core.config import settings
from app.graph.nodes._events import append_event, append_step
from app.graph.policies import consume_budget
from app.graph.state import ReviewState
from app.models.review import AgentAction, AgentActionName, PatchResult
from app.tools.patch_tool import PatchTool

logger = logging.getLogger("RepoGuardian.Node")

_PATCH_TOKEN_RESERVE = 4_096


async def patch_node(state: ReviewState) -> ReviewState:
    """Patch 节点：根据 action 分发到生成 patch 或应用 patch 子流程。

    - generate_patch: 调用 LLM 为可自动修复的问题生成 unified diff
    - apply_patch:  在克隆仓库中 git apply 选中的 patch
    """
    action = AgentAction.model_validate(state.get("next_action") or {
        "action": "generate_patch",
        "reason": "Generate patch.",
    })
    if action.action in {AgentActionName.generate_patch, AgentActionName.revise_patch}:
        logger.info("🩹 [修复] 生成 patch，目标 issue IDs: %s", action.target_issue_ids)
        return await _generate_patch(state, action)
    if action.action == AgentActionName.apply_patch:
        patch_id = action.tool_args.get("patch_id")
        logger.info("🩹 [修复] 应用 patch（patch_id=%s）", patch_id or "自动选择最新")
        return await _apply_patch(state, action)

    message = f"Unsupported patch action: {action.action.value}"
    logger.error("🩹 [修复] 不支持的 patch action: %s", action.action.value)
    return ReviewState(
        agent_events=append_event(state, action.action, action.reason, "failed", message),
        step_progress=append_step(state, "patch", "failed", message),
    )


async def _generate_patch(state: ReviewState, action: AgentAction) -> ReviewState:
    budget = consume_budget(
        state,
        patch_attempts=1,
        model_calls=1,
        token_usage=_PATCH_TOKEN_RESERVE,
    )
    if budget is None:
        message = "补丁或模型调用预算已耗尽"
        return ReviewState(
            agent_events=append_event(state, action.action, action.reason, "completed", message),
            step_progress=append_step(state, "patch_generate", "completed", message),
        )
    provider: Any = state.get("_provider") or build_provider(
        settings.repoguardian_provider,
        settings.openai_api_key,
        settings.openai_base_url,
        settings.repoguardian_model,
    )
    logger.info("🩹 [生成 patch] 调用 LLM 生成修复 diff...")
    patches = await provider.generate_patch(dict(state), state.get("model"))
    patch_dicts = [patch.model_dump(mode="json") for patch in patches]
    previous = list(state.get("patches") or [])
    message = f"Generated {len(patch_dicts)} patch(es)."
    logger.info("🩹 [生成 patch] 完成: 生成了 %d 个 patch（累计 %d 个）", len(patch_dicts), len(previous) + len(patch_dicts))
    return ReviewState(
        patches=previous + patch_dicts,
        execution_budget=budget.model_dump(),
        agent_events=append_event(state, action.action, action.reason, "completed", message),
        step_progress=append_step(state, "patch_generate", "completed", message),
    )


async def _apply_patch(state: ReviewState, action: AgentAction) -> ReviewState:
    """在临时仓库中执行 git apply。先 --check，通过后再正式 apply。"""
    patches = [PatchResult.model_validate(item) for item in state.get("patches") or []]
    patch = _select_patch(patches, action.tool_args.get("patch_id"))
    if patch is None:
        message = "No generated patch is available to apply."
        logger.warning("🩹 [应用 patch] 无可用的 generated patch")
        return ReviewState(
            agent_events=append_event(state, action.action, action.reason, "failed", message),
            step_progress=append_step(state, "patch_apply", "failed", message),
        )

    logger.info("🩹 [应用 patch] git apply patch %s...", patch.id[:8])
    applied = await PatchTool().apply(state.get("repo_path", ""), patch)
    updated = [applied if item.id == applied.id else item for item in patches]
    message = f"Patch {applied.id[:8]} {applied.status}."
    status = "completed" if applied.status == "applied" else "failed"
    if applied.status == "applied":
        logger.info("🩹 [应用 patch] 成功: patch %s 已应用", applied.id[:8])
    else:
        logger.error("🩹 [应用 patch] 失败: patch %s → %s", applied.id[:8], applied.error)
    return ReviewState(
        patches=[item.model_dump(mode="json") for item in updated],
        agent_events=append_event(state, action.action, action.reason, status, message),
        step_progress=append_step(state, "patch_apply", status, message),
    )


def _select_patch(patches: list[PatchResult], patch_id: str | None) -> PatchResult | None:
    """选 patch 策略：优先按 ID 精确匹配，否则选最后一个 status=generated 的。"""
    if patch_id:
        for patch in patches:
            if patch.id == patch_id:
                return patch
        return None
    for patch in reversed(patches):
        if patch.status == "generated":
            return patch
    return None
