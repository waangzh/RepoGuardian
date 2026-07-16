"""受控修复子图：生成、应用、验证，再由 Agent 决定修订或放弃。"""

from langgraph.graph import END, StateGraph

from app.graph.nodes.agent_decide import agent_decide_node
from app.graph.nodes.human_required import human_required_node
from app.graph.nodes.repair_policy import (
    repair_accept_patch_node,
    repair_apply_patch_node,
    repair_abandon_patch_node,
    repair_assessment_node,
    repair_generate_patch_node,
    repair_policy_node,
    repair_validation_node,
)
from app.graph.routers import route_repair_action, route_repair_assessment, route_repair_entry
from app.graph.state import ReviewState


def build_repair_graph() -> StateGraph:
    """构建可返回主图的 repair 子图，不包含报告终点。"""
    graph = StateGraph(ReviewState)
    graph.add_node("repair_policy", repair_policy_node)
    graph.add_node("generate_patch", repair_generate_patch_node)
    graph.add_node("apply_patch", repair_apply_patch_node)
    graph.add_node("validation", repair_validation_node)
    graph.add_node("repair_assessment", repair_assessment_node)
    graph.add_node("repair_decide", agent_decide_node)
    graph.add_node("accept_patch", repair_accept_patch_node)
    graph.add_node("abandon_patch", repair_abandon_patch_node)
    graph.add_node("human_required", human_required_node)
    graph.add_node("repair_exit", lambda state: state)

    graph.set_entry_point("repair_policy")
    graph.add_conditional_edges(
        "repair_policy",
        route_repair_entry,
        {
            "generate_patch": "generate_patch",
            "abandon_patch": "abandon_patch",
            "repair_exit": "repair_exit",
        },
    )
    graph.add_edge("abandon_patch", "repair_exit")
    graph.add_edge("generate_patch", "apply_patch")
    graph.add_edge("apply_patch", "validation")
    graph.add_edge("validation", "repair_assessment")
    graph.add_conditional_edges(
        "repair_assessment",
        route_repair_assessment,
        {
            "apply_patch": "apply_patch",
            "repair_decide": "repair_decide",
            "repair_exit": "repair_exit",
        },
    )
    graph.add_conditional_edges(
        "repair_decide",
        route_repair_action,
        {
            "generate_patch": "generate_patch",
            "accept_patch": "accept_patch",
            "abandon_patch": "abandon_patch",
            "human_required": "human_required",
            "repair_exit": "repair_exit",
        },
    )
    graph.add_edge("accept_patch", "repair_exit")
    graph.add_edge("repair_exit", END)
    graph.add_edge("human_required", "repair_exit")
    return graph
