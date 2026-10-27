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


def route_discovery(state):
    return [Send("discover", {"review_id": state["review_id"], "assignment": item,
                             "goals": [goal for goal in state["goals"] if goal["id"] in item["goal_ids"]]})
            for item in state["assignments"]] or "collect"



def route_verification(state):
    return [Send("verify", {"review_id": state["review_id"], "candidate": item,
                           "related_candidates": comparison_candidates(item, state["eligible_candidates"])})
            for item in state["eligible_candidates"]] or "arbitrate"



def route_investigation(state):
    if state.get("investigation_round", 0):
        return "report"
    pending = {item["candidate_id"] for item in state["decisions"]
               if item["decision"] == "insufficient_evidence"}
    eligible = {item["id"] for item in state["eligible_candidates"]}
    candidates = [item for item in state["candidates"]
                  if item["id"] in pending & eligible and item["severity"] in {"critical", "high"}]
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
