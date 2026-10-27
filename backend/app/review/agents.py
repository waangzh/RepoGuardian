"""Native LangChain agents own their message/tool loops and structured output."""

from pathlib import Path

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import HumanMessage

from app.review.context import AgentContext
from app.review.models import DiscoveryOutput, ReviewLimits, VerificationOutput
from app.review.tools import REVIEW_TOOLS


class ReviewGuardMiddleware(AgentMiddleware):
    def __init__(self, limits):
        self.limits = limits

    async def abefore_model(self, state, runtime):
        if runtime.context.check_active:
            await runtime.context.check_active()
        remaining = self.limits.model_calls - state.get("thread_model_call_count", 0)
        tools = state.get("thread_tool_call_count", {}).get("__all__", 0)
        if remaining <= 2 or tools >= max(0, self.limits.tool_calls - 2):
            message = next((item for item in state["messages"]
                            if item.id == "review-budget-reminder"), None)
            if message:
                return None
            return {"messages": [HumanMessage(
                content=f"执行预算提醒：最多还可调用模型 {remaining} 次。请立即收束调查，"
                    "用最终结构化输出提交已有发现、合格覆盖和未完成限制；"
                    "不要继续重复失败请求，不要将未检查范围标为完成。",
                id="review-budget-reminder")]}


def agent_recursion_limit(limits: ReviewLimits):
    # Model/tool middleware each add graph nodes; call budgets remain the hard limits.
    return limits.model_calls * 8 + 24


def build_agent(model, reviewer: str, limits: ReviewLimits):
    if reviewer not in {"behavior", "contract", "specialist", "verifier", "investigator"}:
        raise ValueError("unknown_reviewer")
    prompt = (Path(__file__).parent / "prompts" / f"{reviewer}.md").read_text(encoding="utf-8")
    schema = VerificationOutput if reviewer in {"verifier", "investigator"} else DiscoveryOutput
    return create_agent(
        model=model, tools=REVIEW_TOOLS, system_prompt=prompt,
        response_format=ToolStrategy(schema),
        middleware=[
            ReviewGuardMiddleware(limits),
            ModelCallLimitMiddleware(run_limit=limits.model_calls,
                                    thread_limit=limits.model_calls, exit_behavior="error"),
            ToolCallLimitMiddleware(run_limit=limits.tool_calls,
                                   thread_limit=limits.tool_calls, exit_behavior="error"),
        ],
        context_schema=AgentContext, name=reviewer,
    )
