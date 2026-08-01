import logging
from hashlib import sha256
import json

from langgraph.types import interrupt

from app.graph.nodes._events import append_event, append_step
from app.graph.state import ReviewState
from app.models.review import AgentAction

logger = logging.getLogger("RepoGuardian.Node")


async def human_required_node(state: ReviewState) -> ReviewState:
    """人工审批节点：暂停当前路径，记录请求后进入报告阶段。

    当前为占位实现，无实际人工参与机制。
    """
    action = AgentAction.model_validate(state.get("next_action") or {
        "action": "request_human",
        "reason": "Human approval is required.",
    })
    request = action.human_request
    assert request is not None
    if state.get("_human_interrupt_enabled"):
        request_payload = request.model_dump(mode="json")
        request_id = sha256(json.dumps(
            {"task_id": state.get("task_id"), "request": request_payload},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")).hexdigest()[:32]
        answer = interrupt({
            "request_id": request_id,
            "reason": action.reason,
            "request": request_payload,
        })
        return ReviewState(
            status="reviewing",
            human_request=request_payload,
            human_answer=answer if isinstance(answer, dict) else {"text": str(answer)},
            next_action=None,
            agent_events=append_event(
                state, action.action, action.reason, "completed", "已收到人工回答，继续审查"
            ),
            step_progress=append_step(
                state, "human_required", "completed", "已收到人工回答，继续审查"
            ),
        )
    message = "已请求人工确认；在收到回答前不会继续自动修复。"
    logger.info("👤 [人工审批] %s", message)
    return ReviewState(
        status="waiting_for_human",
        human_request=request.model_dump(mode="json"),
        repair_enabled=False,
        agent_events=append_event(state, action.action, action.reason, "completed", message),
        step_progress=append_step(state, "human_required", "completed", message),
    )
