from langgraph.graph import END, StateGraph

from app.graph.state import ReviewState


def build_review_graph(phase: int = 2) -> StateGraph:
    graph = StateGraph(ReviewState)

    from app.graph.nodes.intake import intake_node
    from app.graph.nodes.repo_prepare import repo_prepare_node
    from app.graph.nodes.diff_parse import diff_parse_node
    from app.graph.nodes.repo_index import repo_index_node
    from app.graph.nodes.context_retrieve import context_retrieve_node
    from app.graph.nodes.review import review_node
    from app.graph.nodes.report import report_node

    graph.add_node("intake", intake_node)
    graph.add_node("repo_prepare", repo_prepare_node)
    graph.add_node("diff_parse", diff_parse_node)
    graph.add_node("repo_index", repo_index_node)
    graph.add_node("context_retrieve", context_retrieve_node)
    graph.add_node("review", review_node)
    graph.add_node("report", report_node)

    graph.set_entry_point("intake")

    if phase == 2:
        graph.add_edge("intake", "repo_prepare")
        graph.add_edge("repo_prepare", "diff_parse")
        graph.add_edge("diff_parse", "repo_index")
        graph.add_edge("repo_index", "context_retrieve")
        graph.add_edge("context_retrieve", "review")
        graph.add_edge("review", "report")
        graph.add_edge("report", END)

    return graph
