"""Prepare one checkpoint-owned retry without resetting frozen branch budgets."""

from langgraph.types import Overwrite
from app.review.limitations import LIMITATION_CALL_LIMITS


RESULT_STAGES = ("discovery_results", "verification_results", "investigation_results", "limitation_results")


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
            call_limit = (min(limits.model_calls, LIMITATION_CALL_LIMITS[state.get("review_depth", "standard")])
                          if stage == "limitation_results" else limits.model_calls)
            tool_limit = min(limits.tool_calls, 24) if stage == "limitation_results" else limits.tool_calls
            if result["model_calls"] >= call_limit or result["tool_calls"] >= tool_limit:
                raise ValueError("review_retry_budget_exhausted")
            reviewer = (roles[result["id"]] if stage == "discovery_results" else
                        "verifier" if stage == "verification_results" else
                        "limitation_reviewer" if stage == "limitation_results" else "investigator")
            previous.append({"reviewer": reviewer, "result": result})
    if not previous:
        raise ValueError("review_retry_no_failed_branches")
    return {**updates, "retry_count": 1, "retry_results": previous,
            "investigation_round": 0, "limitation_round": 0, "report": {}, "status": "retrying"}
