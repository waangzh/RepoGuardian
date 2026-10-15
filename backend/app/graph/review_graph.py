"""唯一生产审查图：固定顺序推进，引擎由冻结任务决定。"""

from langgraph.graph import END, StateGraph

from app.graph.nodes.diff_parse import diff_parse_node
from app.graph.nodes.intake import intake_node
from app.graph.nodes.project_detection import project_detection_node
from app.graph.nodes.repo_index import repo_index_node
from app.graph.nodes.repo_prepare import repo_prepare_node
from app.graph.nodes.report import complete_node, report_node
from app.graph.nodes.review_units import review_plan_node
from app.graph.nodes.task_review_runtime import task_review_runtime_node
from app.graph.state import ReviewState, ReviewRunContext


def build_review_graph() -> StateGraph:
    """构建 task_review_v1 生产图；legacy_units 旧图已下线。"""
    graph = StateGraph(ReviewState, context_schema=ReviewRunContext)
    for name, node in (("intake", intake_node), ("repo_prepare", repo_prepare_node),
                       ("diff_parse", diff_parse_node), ("repo_index", repo_index_node),
                       ("project_detection", project_detection_node), ("review_plan", review_plan_node),
                       ("task_review_runtime", task_review_runtime_node), ("report", report_node),
                       ("complete", complete_node)):
        graph.add_node(name, node)
    graph.set_entry_point("intake")
    for before, after in (("intake", "repo_prepare"), ("repo_prepare", "diff_parse"),
                          ("diff_parse", "repo_index"), ("repo_index", "project_detection"),
                          ("project_detection", "review_plan"), ("review_plan", "task_review_runtime"),
                          ("task_review_runtime", "report"), ("report", "complete")):
        graph.add_edge(before, after)
    graph.add_edge("complete", END)
    return graph
