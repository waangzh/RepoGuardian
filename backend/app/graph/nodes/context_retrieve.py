"""按模型提出且已校验的结构化计划读取仓库上下文。"""

import json
import logging
from typing import Any

from app.graph.nodes._events import append_event, append_step
from app.graph.policies import consume_budget
from app.graph.state import ReviewState
from app.models.review import AgentAction, ContextRetrievalPlan, ReviewPhase
from app.tools.code_search import CodeSearchTool, ContextRetrievalPlanError

logger = logging.getLogger("RepoGuardian.Node")


async def context_retrieve_node(state: ReviewState) -> ReviewState:
    """执行一次去重的、仅能访问索引资源的检索计划。"""
    action = AgentAction.model_validate(state.get("next_action") or {})
    plan = ContextRetrievalPlan.model_validate(action.tool_args["plan"])
    serialized_plan = json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    history = list(state.get("retrieval_history") or [])

    if any(item.get("plan") == serialized_plan for item in history):
        return _rejected_result(
            state,
            action,
            history,
            serialized_plan,
            "重复检索计划已被去重，未执行工具",
        )

    budget = consume_budget(state, context_retrievals=1)
    if budget is None:
        return ReviewState(
            next_action=None,
            phase=ReviewPhase.discovery,
            discovery_stop_reason="context_budget_exhausted",
            agent_events=append_event(state, action.action, action.reason, "completed", "上下文检索预算已耗尽"),
            step_progress=append_step(state, "context_retrieve", "completed", "上下文检索预算已耗尽"),
        )

    failure_fingerprints = [
        fingerprint
        for snapshot in state.get("validation_snapshots") or []
        for fingerprint in snapshot.get("failure_fingerprints", [])
    ]
    try:
        result = await CodeSearchTool().retrieve_context(
            changed_files=state.get("changed_files") or [],
            symbol_index=state.get("symbol_index") or [],
            file_index=state.get("file_index") or [],
            repo_path=state.get("repo_path", ""),
            plan=plan,
            failure_fingerprints=failure_fingerprints,
        )
    except (ContextRetrievalPlanError, ValueError) as exc:
        return _rejected_result(
            state,
            action,
            history,
            serialized_plan,
            f"检索计划被服务端拒绝：{exc}",
            execution_budget=budget.model_dump(),
        )

    existing = list(state.get("context_snippets") or [])
    existing_ranges = {
        (snippet.get("file"), snippet.get("start_line"), snippet.get("end_line"))
        for snippet in existing
    }
    new_snippets = [
        snippet for snippet in result
        if (snippet.get("file"), snippet.get("start_line"), snippet.get("end_line"))
        not in existing_ranges
    ]
    no_new_rounds = (state.get("retrieval_no_new_rounds") or 0) + 1 if not new_snippets else 0
    history.append({
        "plan": serialized_plan,
        "result_count": len(result),
        "new_snippet_count": len(new_snippets),
        "truncated_count": sum(
            1 for snippet in result if snippet.get("content", "").endswith("...(truncated)")
        ),
        "status": "completed",
    })
    message = f"按计划检索到 {len(result)} 个片段，新增 {len(new_snippets)} 个"
    return ReviewState(
        status="resolving_evidence",
        next_action=None,
        context_snippets=existing + new_snippets,
        retrieval_history=history,
        retrieval_no_new_rounds=no_new_rounds,
        phase=ReviewPhase.discovery,
        execution_budget=budget.model_dump(),
        agent_events=append_event(state, action.action, action.reason, "completed", message),
        step_progress=append_step(state, "context_retrieve", "completed", message),
    )


def _rejected_result(
    state: ReviewState,
    action: AgentAction,
    history: list[dict[str, Any]],
    serialized_plan: str,
    message: str,
    *,
    execution_budget: dict[str, Any] | None = None,
) -> ReviewState:
    """拒绝计划也计为无新增信息，防止模型以无效计划形成循环。"""
    history.append({"plan": serialized_plan, "result_count": 0, "new_snippet_count": 0, "status": "rejected"})
    result: dict[str, Any] = {
        "status": "resolving_evidence",
        "next_action": None,
        "retrieval_history": history,
        "retrieval_no_new_rounds": (state.get("retrieval_no_new_rounds") or 0) + 1,
        "phase": ReviewPhase.discovery,
        "agent_events": append_event(state, action.action, action.reason, "rejected", message),
        "step_progress": append_step(state, "context_retrieve", "completed", message),
    }
    if execution_budget is not None:
        result["execution_budget"] = execution_budget
    return ReviewState(**result)
