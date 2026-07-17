"""主审查图：固定顺序推进，只有受限阶段拥有条件分支。"""

from langgraph.graph import END, StateGraph

from app.graph.nodes.agent_decide import agent_decide_node
from app.graph.nodes.context_retrieve import context_retrieve_node
from app.graph.nodes.diff_parse import diff_parse_node
from app.graph.nodes.human_required import human_required_node
from app.graph.nodes.intake import intake_node
from app.graph.nodes.project_detection import project_detection_node
from app.graph.nodes.repo_index import repo_index_node
from app.graph.nodes.repo_prepare import repo_prepare_node
from app.graph.nodes.report import complete_node, report_node
from app.graph.nodes.review import review_node
from app.graph.nodes.verification import verification_node
from app.graph.repair_graph import build_repair_graph
from app.graph.routers import route_discovery_action
from app.graph.state import ReviewState


def build_review_graph(phase: int | None = None) -> StateGraph:
    """构建阶段一确定性主图；phase 参数仅保留调用兼容性。"""
    graph = StateGraph(ReviewState)
    graph.add_node("intake", intake_node)
    graph.add_node("repo_prepare", repo_prepare_node)
    graph.add_node("diff_parse", diff_parse_node)
    graph.add_node("repo_index", repo_index_node)
    graph.add_node("project_detection", project_detection_node)
    graph.add_node("context_retrieve", context_retrieve_node)
    graph.add_node("discovery_decide", agent_decide_node)
    graph.add_node("review", review_node)
    graph.add_node("human_required", human_required_node)
    graph.add_node("verification", verification_node)
    graph.add_node("repair_graph", build_repair_graph().compile())
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
    graph.add_edge("human_required", "report")
    graph.add_edge("review", "verification")
    graph.add_edge("verification", "repair_graph")
    graph.add_edge("repair_graph", "report")
    graph.add_edge("report", "complete")
    graph.add_edge("complete", END)
    return graph
