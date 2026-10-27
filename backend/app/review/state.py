"""The parent graph stores results and identities, never agent message history."""

from typing import Annotated, TypedDict


def merge_results(left: list[dict], right: list[dict]) -> list[dict]:
    merged = {item["id"]: item for item in left}
    for item in right:
        if item["id"] in merged and merged[item["id"]] != item:
            raise ValueError("conflicting_agent_result")
        merged[item["id"]] = item
    return [merged[key] for key in sorted(merged)]


class ReviewState(TypedDict, total=False):
    review_id: str
    snapshot: dict
    goals: list[dict]
    assignments: list[dict]
    discovery_results: Annotated[list[dict], merge_results]
    candidates: list[dict]
    eligible_candidates: list[dict]
    gate_rejections: list[dict]
    verification_results: Annotated[list[dict], merge_results]
    investigation_results: Annotated[list[dict], merge_results]
    decisions: list[dict]
    findings: list[dict]
    investigation_round: int
    report: dict
    status: str
    assignment: dict
    candidate: dict
    related_candidates: list[dict]
    retry_count: int
    retry_results: list[dict]
