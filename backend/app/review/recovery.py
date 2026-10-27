"""Prepare one checkpoint-owned retry without resetting frozen branch budgets."""

from langgraph.types import Overwrite


RESULT_STAGES = ("discovery_results", "verification_results", "investigation_results")


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
                        "verifier" if stage == "verification_results" else "investigator")
            previous.append({"reviewer": reviewer, "result": result})
    if not previous:
        raise ValueError("review_retry_no_failed_branches")
    return {**updates, "retry_count": 1, "retry_results": previous,
            "investigation_round": 0, "report": {}, "status": "retrying"}
