"""LangChain owns the tool loop; each actual model request needs a durable ticket."""

import asyncio
import json
from pathlib import Path

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from app.review.context import AgentContext
from app.review.domain import UnitCompletion, VerificationOutput
from app.review.findings import admit_verification
from app.review.planner import tokens
from app.review.store import BudgetExceeded, LeaseLost, ReviewStopped
from app.review.tools import READ_TOOLS, read_discussion, submit_candidate


class RequestGate(AgentMiddleware):
    async def awrap_model_call(self, request, handler):
        context = request.runtime.context
        await context.check_active()
        tools = request.tools
        if context.tool_calls >= context.provider.run.configuration.options.limits.tool_calls:
            tools = []
        request = request.override(tools=tools)
        schema = getattr(request.response_format, "schema", None)
        estimate = tokens({
            "messages": [message.model_dump(mode="json") for message in
                         ([request.system_message] if request.system_message else []) + request.messages],
            "tools": [convert_to_openai_tool(tool) for tool in tools],
            "response_format": schema.model_json_schema() if schema else None,
        }) + 512
        ticket = await context.budget.reserve(context.work, estimate)
        pending, hunks = dict(context.pending), dict(context.pending_hunks)
        try:
            response = await handler(request)
        except asyncio.CancelledError:
            raise
        except Exception:
            await context.budget.settle(ticket, None)
            raise
        message = next((item for item in reversed(response.result) if isinstance(item, AIMessage)), None)
        await context.budget.settle(ticket, getattr(message, "usage_metadata", None))
        await context.check_active()
        # A successful response proves the model received the full request packet.
        await context.acknowledge(pending, hunks)
        return response


class AgentRunner:
    def __init__(self, model):
        self.model = model

    async def run(self, work, provider, budget):
        context = AgentContext(provider=provider, work=work, budget=budget)
        candidate = None
        try:
            if work.kind == "verification":
                candidate = next(item for item in await asyncio.to_thread(
                    provider.store.candidates, provider.run.id) if item.id == work.candidate_id)
            initial = await provider.initial(context, candidate)
            role = "reviewer" if work.kind == "review" else "verifier"
            tools = list(READ_TOOLS)
            if role == "reviewer":
                tools.append(submit_candidate)
            if provider.run.configuration.options.include_pr_discussions and provider.discussions:
                tools.append(read_discussion)
            prompt = (Path(__file__).parent / "prompts" / f"{role}.md").read_text(encoding="utf-8")
            schema = UnitCompletion if role == "reviewer" else VerificationOutput
            agent = create_agent(self.model, tools=tools, system_prompt=prompt,
                response_format=ToolStrategy(schema), middleware=[RequestGate()],
                context_schema=AgentContext, name=role)
            limits = provider.run.configuration.options.limits
            result = await agent.ainvoke(
                {"messages": [{"role": "user", "content": json.dumps(initial, ensure_ascii=False)}]},
                context=context, config={"recursion_limit": limits.model_calls * 8 + 24})
            output = result.get("structured_response")
            if not isinstance(output, schema):
                raise ValueError("missing_structured_result")
            await budget.active()
            if candidate:
                candidate = admit_verification(candidate, output, context)
            # ainvoke only returns after all tool tasks and their DB receipts finish.
            await asyncio.to_thread(provider.store.finish_work, provider.run.id, work,
                "completed", output.model_dump(), None, provider.lease,
                candidate=candidate, tool_calls=context.tool_calls)
        except (LeaseLost, ReviewStopped, asyncio.CancelledError):
            raise
        except Exception as exc:
            code = str(exc) if isinstance(exc, BudgetExceeded) else "agent_execution_failed"
            await asyncio.to_thread(provider.store.finish_work, provider.run.id, work,
                "interrupted" if isinstance(exc, BudgetExceeded) else "failed", None, code,
                provider.lease, tool_calls=context.tool_calls)
