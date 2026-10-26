"""Native LangChain agents own their message/tool loops and structured output."""

from pathlib import Path

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain.agents.structured_output import ToolStrategy

from app.review.context import AgentContext
from app.review.models import DiscoveryOutput, ReviewLimits, VerificationOutput
from app.review.tools import REVIEW_TOOLS


def build_agent(model, reviewer: str, limits: ReviewLimits):
    if reviewer not in {"behavior", "contract", "specialist", "verifier", "investigator"}:
        raise ValueError("unknown_reviewer")
    prompt = (Path(__file__).parent / "prompts" / f"{reviewer}.md").read_text(encoding="utf-8")
    schema = VerificationOutput if reviewer in {"verifier", "investigator"} else DiscoveryOutput
    return create_agent(
        model=model, tools=REVIEW_TOOLS, system_prompt=prompt,
        response_format=ToolStrategy(schema),
        middleware=[
            ModelCallLimitMiddleware(run_limit=limits.model_calls, exit_behavior="error"),
            ToolCallLimitMiddleware(run_limit=limits.tool_calls, exit_behavior="error"),
        ],
        context_schema=AgentContext, name=reviewer,
    )
