"""Production graph assembly and routing for native multi-agent review."""

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app.review.context import ReviewContext
from app.review.findings import comparison_candidates
from app.review.nodes.preparation import prepare, plan
from app.review.nodes.discovery import discover, collect
from app.review.nodes.verification import (
    evidence_gate, verify, arbitrate, investigate, finish_investigation,
)
from app.review.nodes.reporting import report
from app.review.state import ReviewState
from app.review.verification_batches import verification_batches


def route_discovery(state):
    completed = {item["id"] for item in state.get("discovery_results", [])
                 if item["status"] == "completed"}
    discovery_agent_ids = [item["id"] for item in state["assignments"]]
    return [Send("discover", {"review_id": state["review_id"], "assignment": item,
                             "discovery_agent_ids": discovery_agent_ids,
                             "goals": [goal for goal in state["goals"] if goal["id"] in item["goal_ids"]]})
            for item in state["assignments"] if item["id"] not in completed] or "collect"



def route_verification(state):
    completed = {item["id"] for item in state.get("verification_results", [])
                 if item["status"] == "completed"}
    pending = []
    for batch in state.get("verification_batches", verification_batches(state["eligible_candidates"])):
        candidates = [item for item in batch["candidates"] if item["id"] not in completed]
        if candidates:
            pending.append(Send("verify", {"review_id": state["review_id"],
                "candidate": candidates[0], "batch_id": batch["id"], "batch_candidates": candidates,
                "related_candidates": comparison_candidates(candidates[0], state["eligible_candidates"])}))
    return pending or "arbitrate"



INVESTIGATION_SEVERITIES = {
    "quick": frozenset(),
    "standard": frozenset({"critical", "high"}),
    "deep": frozenset({"critical", "high", "medium"}),
}


def route_investigation(state):
    if state.get("investigation_round", 0):
        return "report"
    severities = INVESTIGATION_SEVERITIES.get(state.get("review_depth", "standard"), frozenset())
    pending = {item["candidate_id"] for item in state["decisions"]
               if item["decision"] == "insufficient_evidence"}
    eligible = {item["id"] for item in state["eligible_candidates"]}
    completed = {item["id"] for item in state.get("investigation_results", [])
                 if item["status"] == "completed"}
    candidates = [item for item in state["candidates"]
                  if item["id"] in (pending & eligible) - completed
                  and item["severity"] in severities]
    return [Send("investigate", {"review_id": state["review_id"], "candidate": item,
                                "related_candidates": comparison_candidates(item, state["eligible_candidates"])})
            for item in candidates] or "report"



def build_review_graph(checkpointer=None):
    graph = StateGraph(ReviewState, context_schema=ReviewContext)
    for name, node in (
        ("prepare", prepare), ("plan", plan), ("discover", discover), ("collect", collect),
        ("evidence_gate", evidence_gate), ("verify", verify), ("arbitrate", arbitrate),
        ("investigate", investigate), ("finish_investigation", finish_investigation), ("report", report),
    ):
        graph.add_node(name, node)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "plan")
    graph.add_conditional_edges("plan", route_discovery, ["discover", "collect"])
    graph.add_edge("discover", "collect")
    graph.add_edge("collect", "evidence_gate")
    graph.add_conditional_edges("evidence_gate", route_verification, ["verify", "arbitrate"])
    graph.add_edge("verify", "arbitrate")
    graph.add_conditional_edges("arbitrate", route_investigation, ["investigate", "report"])
    graph.add_edge("investigate", "finish_investigation")
    graph.add_edge("finish_investigation", "arbitrate")
    graph.add_edge("report", END)
    return graph.compile(checkpointer=checkpointer)
