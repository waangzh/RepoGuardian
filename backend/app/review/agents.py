"""Native LangChain agents own their message/tool loops and structured output."""

from pathlib import Path
import asyncio
import json
from typing import NotRequired

from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware, AgentState, ExtendedModelResponse, ModelCallLimitMiddleware, ToolCallLimitMiddleware,
)
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.types import Command

from app.review.budget import ReviewBudgetExceeded
from app.review.context import AgentContext
from app.review.models import BatchVerificationOutput, DiscoveryOutput, ReviewLimits, VerificationOutput
from app.review.tools import REVIEW_TOOLS


class ReviewAgentState(AgentState):
    review_finalizing: NotRequired[bool]
    review_final_requests: NotRequired[int]


class ReviewGuardMiddleware(AgentMiddleware):
    state_schema = ReviewAgentState

    def __init__(self, limits):
        self.limits = limits

    async def final_state(self, state, context):
        finalizing = state.get("review_finalizing", False)
        requests = state.get("review_final_requests", 0)
        if context.budget is not None:
            branch = await context.budget.branch_state(context.budget_agent_id)
            finalizing = finalizing or branch["finalizing"]
            requests = max(requests, branch["final_requests"])
        return finalizing, requests

    async def abefore_model(self, state, runtime):
        if runtime.context.check_active:
            await runtime.context.check_active()
        remaining = self.limits.model_calls - state.get("thread_model_call_count", 0)
        tools = state.get("thread_tool_call_count", {}).get("__all__", 0)
        closing = remaining <= min(3, self.limits.model_calls) or tools >= max(0, self.limits.tool_calls - 2)
        if runtime.context.budget is not None:
            summary = await runtime.context.budget.summary()
            closing = closing or (summary["committed_input_tokens"] >= summary["input_token_limit"] * 4 // 5)
        if closing:
            message = next((item for item in state["messages"]
                            if item.id == "review-budget-reminder"), None)
            if message:
                return None
            return {"messages": [HumanMessage(
                content=f"执行预算提醒：最多还可调用模型 {remaining} 次。请立即收束调查，"
                    "用最终结构化输出提交已有发现、合格覆盖和未完成限制；"
                    "不要继续重复失败请求，不要将未检查范围标为完成。",
                id="review-budget-reminder")]}

    async def awrap_model_call(self, request, handler):
        context = request.runtime.context
        tools = [tool for tool in request.tools
                 if context.discussions_enabled and context.discussions
                 or (tool.name if hasattr(tool, "name") else tool.get("name")) != "read_discussion"]
        remaining = self.limits.model_calls - request.state.get("thread_model_call_count", 0)
        calls = request.state.get("thread_tool_call_count", {}).get("__all__", 0)
        round_closing = False
        if context.budget is not None:
            summary = await context.budget.summary()
            round_closing = summary["model_call_limit"] - summary["model_calls"] <= 2
        persisted, final_requests = await self.final_state(request.state, context)
        finalizing = persisted or ((self.limits.model_calls >= 3 and remaining <= 2)
                                         or calls >= self.limits.tool_calls - 1 or round_closing)
        if finalizing:
            tools = []  # ToolStrategy adds the final schema tool; no further exploration.
        request = request.override(tools=tools)
        if finalizing and final_requests >= 2:
            raise FinalizationRequired("finalization_attempts_exhausted")

        def encoded(visible):
            return len(json.dumps({
                "messages": [message.model_dump(mode="json") for message in
                             ([request.system_message] if request.system_message else []) + request.messages],
                "tools": [convert_to_openai_tool(tool) for tool in visible],
                "response_format": (request.response_format.schema.model_json_schema()
                    if hasattr(getattr(request.response_format, "schema", None), "model_json_schema")
                    else str(request.response_format)),
            }, ensure_ascii=False).encode("utf-8"))

        ticket = None
        scope = ({"discovery_agent_ids": context.discovery_agent_ids}
                 if context.discovery_agent_ids else {})
        try:
            # An exploring request must leave room for this branch's final submission
            # and one schema correction, or it cannot conclude at all.
            if context.budget is not None:
                ticket = await context.budget.reserve(context.budget_agent_id, encoded(tools),
                    stage="finalize" if finalizing else "explore",
                    hold=max(0, 1 - final_requests) if finalizing else 2, **scope)
        except ReviewBudgetExceeded:
            if finalizing:
                raise
            finalizing, tools = True, []
            request = request.override(tools=tools)
            ticket = await context.budget.reserve(context.budget_agent_id, encoded(tools),
                stage="finalize", hold=max(0, 1 - final_requests), **scope)
        try:
            response = await handler(request)
        except asyncio.CancelledError:
            raise  # The durable outstanding reservation remains unknown and occupied.
        except Exception:
            if ticket is not None:
                await context.budget.settle(ticket, None)
            raise
        message = next((item for item in reversed(response.result) if isinstance(item, AIMessage)), None)
        if ticket is not None:
            await context.budget.settle(ticket, getattr(message, "usage_metadata", None))
        return ExtendedModelResponse(model_response=response, command=Command(update={
            "review_finalizing": finalizing,
            "review_final_requests": final_requests + int(finalizing),
        }))

    async def awrap_tool_call(self, request, handler):
        calls = request.state.get("thread_model_call_count", 0)
        tools = request.state.get("thread_tool_call_count", {}).get("__all__", 0)
        context = request.runtime.context
        round_closing = False
        if context.budget is not None:
            summary = await context.budget.summary()
            round_closing = summary["model_call_limit"] - summary["model_calls"] < 2
        name = (request.tool_call.get("name") if isinstance(request.tool_call, dict)
                else getattr(request.tool_call, "name", None))
        finalizing, _ = await self.final_state(request.state, context)
        if name not in FINAL_SCHEMAS and ((self.limits.model_calls >= 3 and self.limits.model_calls - calls < 2)
                or tools > self.limits.tool_calls - 1 or round_closing
                or finalizing):
            raise FinalizationRequired("finalization_required")
        return await handler(request)


class FinalizationRequired(RuntimeError):
    pass


FINAL_SCHEMAS = {"DiscoveryOutput", "VerificationOutput", "BatchVerificationOutput"}


def agent_recursion_limit(limits: ReviewLimits):
    # Model/tool middleware each add graph nodes; call budgets remain the hard limits.
    return limits.model_calls * 8 + 24


def build_agent(model, reviewer: str, limits: ReviewLimits, *, batch=False):
    if reviewer not in {"reviewer", "contract", "specialist", "verifier", "investigator"}:
        raise ValueError("unknown_reviewer")
    prompt = (Path(__file__).parent / "prompts" / f"{reviewer}.md").read_text(encoding="utf-8")
    schema = (BatchVerificationOutput if batch else VerificationOutput
              if reviewer in {"verifier", "investigator"} else DiscoveryOutput)
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
