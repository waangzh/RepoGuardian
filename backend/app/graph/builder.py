from langgraph.graph import END, StateGraph

from app.graph.state import ReviewState
from app.models.review import AgentActionName


def build_review_graph(phase: int = 2) -> StateGraph:
    graph = StateGraph(ReviewState)

    from app.graph.nodes.intake import intake_node
    from app.graph.nodes.repo_prepare import repo_prepare_node
    from app.graph.nodes.diff_parse import diff_parse_node
    from app.graph.nodes.repo_index import repo_index_node
    from app.graph.nodes.agent_decide import agent_decide_node
    from app.graph.nodes.context_retrieve import context_retrieve_node
    from app.graph.nodes.static_analysis import static_analysis_node
    from app.graph.nodes.review import review_node
    from app.graph.nodes.patch import patch_node
    from app.graph.nodes.test import test_node
    from app.graph.nodes.human_required import human_required_node
    from app.graph.nodes.report import report_node

    graph.add_node("intake", intake_node)
    graph.add_node("repo_prepare", repo_prepare_node)
    graph.add_node("diff_parse", diff_parse_node)
    graph.add_node("repo_index", repo_index_node)
    graph.add_node("agent_decide", agent_decide_node)
    graph.add_node("context_retrieve", context_retrieve_node)
    graph.add_node("static_analysis", static_analysis_node)
    graph.add_node("review", review_node)
    graph.add_node("patch", patch_node)
    graph.add_node("test", test_node)
    graph.add_node("human_required", human_required_node)
    graph.add_node("report", report_node)

    graph.set_entry_point("intake")

    graph.add_edge("intake", "repo_prepare")
    graph.add_edge("repo_prepare", "diff_parse")
    graph.add_edge("diff_parse", "repo_index")
    graph.add_edge("repo_index", "agent_decide")
    graph.add_conditional_edges(
        "agent_decide",
        route_agent_action,
        {
            AgentActionName.retrieve_context.value: "context_retrieve",
            AgentActionName.run_static_analysis.value: "static_analysis",
            AgentActionName.review_code.value: "review",
            AgentActionName.generate_patch.value: "patch",
            AgentActionName.apply_patch.value: "patch",
            AgentActionName.run_tests.value: "test",
            AgentActionName.finish_report.value: "report",
            AgentActionName.request_human.value: "human_required",
        },
    )
    graph.add_edge("context_retrieve", "agent_decide")
    graph.add_edge("static_analysis", "agent_decide")
    graph.add_edge("review", "agent_decide")
    graph.add_edge("patch", "agent_decide")
    graph.add_edge("test", "agent_decide")
    graph.add_edge("human_required", "report")
    graph.add_edge("report", END)

    return graph


def route_agent_action(state: ReviewState) -> str:
    action = (state.get("next_action") or {}).get("action")
    allowed = {item.value for item in AgentActionName}
    if action in allowed:
        return action
    return AgentActionName.finish_report.value
