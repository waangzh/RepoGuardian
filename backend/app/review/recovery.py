"""Prepare one checkpoint-owned retry without resetting frozen branch budgets."""

from langgraph.types import Overwrite
from app.review.findings import collect_candidates
from app.review.limitations import limitation_discoveries


RESULT_STAGES = ("discovery_results", "verification_results", "investigation_results")
RETIRED_LIMITATION_NODES = frozenset({"plan_limitations", "review_limitation", "collect_limitations"})


async def advance_retired_limitation_checkpoint(graph, config, state, context):
    """Retire only the old scheduled stage; keep every result and budget entry."""
    scheduled = set(state.next) | {task.name for task in state.tasks}
    if not scheduled & RETIRED_LIMITATION_NODES:
        return state
    if not scheduled <= RETIRED_LIMITATION_NODES:
        raise ValueError("review_checkpoint_stage_changed")
    await context.active()
    candidates = collect_candidates(state.values.get("discovery_results", []) + limitation_discoveries(
        state.values.get("limitation_results", [])))
    if context.budget is not None:
        for batch in state.values.get("limitation_batches", []):
            await context.budget.finish(batch["id"])
    await context.active()
    updates = {key: Overwrite(state.values[key]) for key in (*RESULT_STAGES, "limitation_results")
               if key in state.values}
    await graph.aupdate_state(config, {**updates, "candidates": candidates}, as_node="collect")
    return await graph.aget_state(config)


def retry_updates(state, limits):
    if state.get("retry_count", 0):
        raise ValueError("review_retry_limit")
    roles = {item["id"]: item["reviewer"] for item in state["assignments"]}
    previous = []
    updates = {}
    for stage in RESULT_STAGES:
        results = state.get(stage, [])
        updates[stage] = Overwrite([item for item in results if item["status"] == "completed"])
        for result in results:
            if result["status"] != "failed":
                continue
            if result.get("model_calls") is None or result.get("tool_calls") is None:
                raise ValueError("review_retry_usage_unknown")
            if result["model_calls"] >= limits.model_calls or result["tool_calls"] >= limits.tool_calls:
                raise ValueError("review_retry_budget_exhausted")
            reviewer = (roles[result["id"]] if stage == "discovery_results" else
                        "verifier" if stage == "verification_results" else
                        "investigator")
            previous.append({"reviewer": reviewer, "result": result})
    if not previous and not any(item["status"] == "failed" for item in state.get("limitation_results", [])):
        raise ValueError("review_retry_no_failed_branches")
    return {**updates, "retry_count": 1, "retry_results": previous,
            "investigation_round": 0, "report": {}, "status": "retrying"}
