"""主审查图：固定顺序推进，只有受限阶段拥有条件分支。"""

from langgraph.graph import END, StateGraph

from app.graph.nodes.agent_decide import agent_decide_node
from app.graph.nodes.context_retrieve import context_retrieve_node
from app.graph.nodes.diff_parse import diff_parse_node
from app.graph.nodes.human_required import human_required_node
from app.graph.nodes.intake import intake_node
from app.graph.nodes.issue_validation import (
    issue_deduplication_node,
    issue_policy_node,
    issue_verifier_node,
)
from app.graph.nodes.project_detection import project_detection_node
from app.graph.nodes.repo_index import repo_index_node
from app.graph.nodes.repo_prepare import repo_prepare_node
from app.graph.nodes.report import complete_node, report_node
from app.graph.nodes.review import review_node
from app.graph.nodes.review_units import review_plan_node, review_units_node
from app.graph.nodes.resolve_evidence import resolve_evidence_node
from app.graph.routers import route_discovery_action
from app.graph.state import ReviewState


def build_review_graph(phase: int | None = None) -> StateGraph:
    """构建阶段一确定性主图；phase 参数仅保留调用兼容性。"""
    if phase == 2:
        return _build_review_unit_graph()
    graph = StateGraph(ReviewState)
    graph.add_node("intake", intake_node)
    graph.add_node("repo_prepare", repo_prepare_node)
    graph.add_node("diff_parse", diff_parse_node)
    graph.add_node("repo_index", repo_index_node)
    graph.add_node("project_detection", project_detection_node)
    graph.add_node("context_retrieve", context_retrieve_node)
    graph.add_node("discovery_decide", agent_decide_node)
    graph.add_node("review", review_node)
    graph.add_node("resolve_evidence", resolve_evidence_node)
    graph.add_node("issue_policy", issue_policy_node)
    graph.add_node("issue_verifier", issue_verifier_node)
    graph.add_node("issue_deduplication", issue_deduplication_node)
    graph.add_node("human_required", human_required_node)
    graph.add_node("report", report_node)
    graph.add_node("complete", complete_node)

    graph.set_entry_point("intake")
    graph.add_edge("intake", "repo_prepare")
    graph.add_edge("repo_prepare", "diff_parse")
    graph.add_edge("diff_parse", "repo_index")
    graph.add_edge("repo_index", "project_detection")
    # Repository detection is metadata-only. Baseline/static validation stays
    # available to explicit validation backends, never on the read-only path.
    graph.add_edge("project_detection", "discovery_decide")
    graph.add_conditional_edges(
        "discovery_decide",
        route_discovery_action,
        {
            "context_retrieve": "context_retrieve",
            "review": "review",
            "human_required": "human_required",
        },
    )
    graph.add_edge("context_retrieve", "discovery_decide")
    graph.add_conditional_edges(
        "human_required",
        lambda state: (
            "report" if state.get("status") == "waiting_for_human" else "discovery_decide"
        ),
        {"report": "report", "discovery_decide": "discovery_decide"},
    )
    graph.add_edge("review", "resolve_evidence")
    graph.add_edge("resolve_evidence", "issue_policy")
    graph.add_edge("issue_policy", "issue_verifier")
    graph.add_edge("issue_verifier", "issue_deduplication")
    # Dynamic validation and repair are deliberately outside the production
    # review plane.  The main graph terminates after static issue aggregation.
    graph.add_edge("issue_deduplication", "report")
    graph.add_edge("report", "complete")
    graph.add_edge("complete", END)
    return graph


def _build_review_unit_graph() -> StateGraph:
    """阶段二主图：确定性拆分、Unit 独立执行、稳定聚合。"""
    graph = StateGraph(ReviewState)
    graph.add_node("intake", intake_node)
    graph.add_node("repo_prepare", repo_prepare_node)
    graph.add_node("diff_parse", diff_parse_node)
    graph.add_node("repo_index", repo_index_node)
    graph.add_node("project_detection", project_detection_node)
    graph.add_node("review_plan", review_plan_node)
    graph.add_node("review_units", review_units_node)
    graph.add_node("human_required", human_required_node)
    graph.add_node("resolve_evidence", resolve_evidence_node)
    graph.add_node("issue_policy", issue_policy_node)
    graph.add_node("issue_verifier", issue_verifier_node)
    graph.add_node("issue_deduplication", issue_deduplication_node)
    graph.add_node("report", report_node)
    graph.add_node("complete", complete_node)

    graph.set_entry_point("intake")
    graph.add_edge("intake", "repo_prepare")
    graph.add_edge("repo_prepare", "diff_parse")
    graph.add_edge("diff_parse", "repo_index")
    graph.add_edge("repo_index", "project_detection")
    graph.add_edge("project_detection", "review_plan")
    graph.add_edge("review_plan", "review_units")
    graph.add_conditional_edges(
        "review_units",
        lambda state: (
            "report" if state.get("status") == "failed"
            else "human_required" if (state.get("next_action") or {}).get("action") == "request_human"
            else "resolve_evidence"
        ),
        {
            "report": "report",
            "human_required": "human_required",
            "resolve_evidence": "resolve_evidence",
        },
    )
    graph.add_conditional_edges(
        "human_required",
        lambda state: "report" if state.get("status") == "waiting_for_human" else "review_units",
        {"report": "report", "review_units": "review_units"},
    )
    graph.add_edge("resolve_evidence", "issue_policy")
    graph.add_edge("issue_policy", "issue_verifier")
    graph.add_edge("issue_verifier", "issue_deduplication")
    graph.add_edge("issue_deduplication", "report")
    graph.add_edge("report", "complete")
    graph.add_edge("complete", END)
    return graph
