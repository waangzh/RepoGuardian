import logging

from langgraph.graph import END, StateGraph

from app.graph.state import ReviewState
from app.models.review import AgentActionName

logger = logging.getLogger("RepoGuardian.Graph")


def build_review_graph(phase: int = 2) -> StateGraph:
    """构建 LangGraph 审查流程图。

    图结构：
        确定性准备阶段（线性链）:
          intake → repo_prepare → diff_parse → repo_index → agent_decide

        Agent 决策循环（条件路由 + 反馈边）:
          agent_decide ─┬→ context_retrieve ──→ agent_decide
                        ├→ static_analysis ──→ agent_decide
                        ├→ review ────────────→ agent_decide
                        ├→ patch ─────────────→ agent_decide
                        ├→ test ──────────────→ agent_decide
                        ├→ human_required ────→ report → END
                        └→ report ────────────→ END
    """  # noqa: E501
    logger.info("🔧 构建审查流程图（phase=%d）", phase)
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

    # ---- 注册所有节点 ----
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

    # ---- 确定性准备阶段（线性边）----
    graph.set_entry_point("intake")
    graph.add_edge("intake", "repo_prepare")
    graph.add_edge("repo_prepare", "diff_parse")
    graph.add_edge("diff_parse", "repo_index")
    graph.add_edge("repo_index", "agent_decide")

    # ---- Agent 决策循环（条件路由）----
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

    # ---- 反馈边：工具节点执行完毕后回到 agent_decide ----
    graph.add_edge("context_retrieve", "agent_decide")
    graph.add_edge("static_analysis", "agent_decide")
    graph.add_edge("review", "agent_decide")
    graph.add_edge("patch", "agent_decide")
    graph.add_edge("test", "agent_decide")

    # ---- 终止边 ----
    graph.add_edge("human_required", "report")
    graph.add_edge("report", END)

    logger.info("✅ 审查流程图构建完成（12 个节点，1 个条件分支）")
    return graph


def route_agent_action(state: ReviewState) -> str:
    """根据 LLM 决策结果路由到下一个工具节点。"""
    action = (state.get("next_action") or {}).get("action")
    allowed = {item.value for item in AgentActionName}
    if action in allowed:
        logger.debug("🧭 路由决策: %s → %s", action, action)
        return action
    # 非法 action 回退到 finish_report 兜底
    logger.warning("⚠️ 未知 action '%s'，回退到 finish_report", action)
    return AgentActionName.finish_report.value
