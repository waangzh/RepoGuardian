import pytest
from pydantic import ValidationError

from app.graph.builder import route_agent_action
from app.models.review import AgentAction


def test_agent_action_accepts_valid_json_shape() -> None:
    action = AgentAction.model_validate({
        "action": "retrieve_context",
        "reason": "需要更多上下文",
        "target_issue_ids": [],
        "tool_args": {},
    })

    assert action.action == "retrieve_context"


def test_agent_action_rejects_unknown_action() -> None:
    with pytest.raises(ValidationError):
        AgentAction.model_validate({"action": "unknown", "reason": "bad"})


def test_route_agent_action_uses_next_action() -> None:
    route = route_agent_action({"next_action": {"action": "run_tests"}})

    assert route == "run_tests"


def test_route_agent_action_falls_back_to_report() -> None:
    route = route_agent_action({"next_action": {"action": "unknown"}})

    assert route == "finish_report"
