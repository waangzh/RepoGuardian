from typing import Any

from app.agents.providers import build_provider
from app.core.config import settings
from app.graph.nodes._events import append_event, append_step
from app.graph.state import ReviewState
from app.models.review import AgentAction, AgentActionName, PatchResult
from app.tools.patch_tool import PatchTool


async def patch_node(state: ReviewState) -> ReviewState:
    action = AgentAction.model_validate(state.get("next_action") or {
        "action": "generate_patch",
        "reason": "Generate patch.",
    })
    if action.action == AgentActionName.generate_patch:
        return await _generate_patch(state, action)
    if action.action == AgentActionName.apply_patch:
        return await _apply_patch(state, action)

    message = f"Unsupported patch action: {action.action.value}"
    return ReviewState(
        agent_events=append_event(state, action.action, action.reason, "failed", message),
        step_progress=append_step(state, "patch", "failed", message),
    )


async def _generate_patch(state: ReviewState, action: AgentAction) -> ReviewState:
    provider: Any = state.get("_provider") or build_provider(
        settings.repoguardian_provider,
        settings.openai_api_key,
        settings.openai_base_url,
        settings.repoguardian_model,
    )
    patches = await provider.generate_patch(dict(state), state.get("model"))
    patch_dicts = [patch.model_dump(mode="json") for patch in patches]
    previous = list(state.get("patches") or [])
    message = f"Generated {len(patch_dicts)} patch(es)."
    return ReviewState(
        patches=previous + patch_dicts,
        agent_events=append_event(state, action.action, action.reason, "completed", message),
        step_progress=append_step(state, "patch_generate", "completed", message),
    )


async def _apply_patch(state: ReviewState, action: AgentAction) -> ReviewState:
    patches = [PatchResult.model_validate(item) for item in state.get("patches") or []]
    patch = _select_patch(patches, action.tool_args.get("patch_id"))
    if patch is None:
        message = "No generated patch is available to apply."
        return ReviewState(
            agent_events=append_event(state, action.action, action.reason, "failed", message),
            step_progress=append_step(state, "patch_apply", "failed", message),
        )

    applied = await PatchTool().apply(state.get("repo_path", ""), patch)
    updated = [applied if item.id == applied.id else item for item in patches]
    message = f"Patch {applied.id[:8]} {applied.status}."
    status = "completed" if applied.status == "applied" else "failed"
    return ReviewState(
        patches=[item.model_dump(mode="json") for item in updated],
        agent_events=append_event(state, action.action, action.reason, status, message),
        step_progress=append_step(state, "patch_apply", status, message),
    )


def _select_patch(patches: list[PatchResult], patch_id: str | None) -> PatchResult | None:
    if patch_id:
        for patch in patches:
            if patch.id == patch_id:
                return patch
        return None
    for patch in reversed(patches):
        if patch.status == "generated":
            return patch
    return None
