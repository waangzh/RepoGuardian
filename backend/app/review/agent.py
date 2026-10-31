"""LangChain owns the tool loop; each actual model request needs a durable ticket."""

import asyncio
import json
import logging
from pathlib import Path
from time import perf_counter

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelResponse, hook_config
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from app.core.config import settings
from app.review.context import AgentContext, NoProgressDetected
from app.review.domain import UnitCompletion, VerificationOutput, utcnow
from app.review.findings import admit_verification
from app.review.planner import tokens, unit_capacity
from app.review.store import BudgetExceeded, LeaseLost, ReviewStopped, verification_input_limit
from app.review.tools import READ_TOOLS, read_discussion, submit_candidate

logger = logging.getLogger("RepoGuardian.Review.Agent")


class MissingStructuredResult(ValueError):
    pass


def request_input_tokens(messages, tools, schema):
    return tokens({
        "messages": [message.model_dump(mode="json") for message in messages],
        "tools": [convert_to_openai_tool(tool) for tool in tools],
        "response_format": schema.model_json_schema(),
    }) + 512


class RequestGate(AgentMiddleware):
    async def awrap_tool_call(self, request, handler):
        context = request.runtime.context
        await context.check_active()
        result = await handler(request)
        # Schema and unknown-tool errors happen before @bounded executes.
        if isinstance(result, ToolMessage) and result.status == "error":
            context.tool_calls += 1
            fields = request.tool.tool_call_schema.model_fields if request.tool else {}
            error = ("tool_call_limit"
                     if context.tool_calls > context.provider.run.configuration.options.limits.tool_calls
                     else "invalid_tool_arguments" if request.tool else "unknown_tool")
            body = {
                "ok": False, "error": error,
                "required_arguments": [name for name, field in fields.items() if field.is_required()],
                "next_action": "Correct the arguments using the tool schema. Do not repeat "
                               "the same rejected call. Return the required result tool if finished.",
            }
            context.note_tool_result(request.tool_call["name"], request.tool_call["args"], body)
            result = ToolMessage(content=json.dumps(body), tool_call_id=request.tool_call["id"],
                                 name=request.tool_call["name"], status="error")
        await context.check_active()
        return result

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state, runtime):
        if state.get("structured_response") is not None:
            return None
        message = state["messages"][-1]
        if not isinstance(message, AIMessage) or message.tool_calls:
            return None
        context = runtime.context
        if (context.structured_output_retry
                or context.model_calls >= context.provider.run.configuration.options.limits.model_calls):
            raise MissingStructuredResult
        context.structured_output_retry = True
        schema_name = "UnitCompletion" if context.work.kind == "review" else "VerificationOutput"
        logger.warning(
            "模型未交付结构化结果，将在原预算内纠正一次 review_id=%.8s work_id=%.8s expected=%s",
            context.provider.run.id, context.work.id, schema_name)
        return {"messages": [HumanMessage(content=(
            f"Your previous reply did not call the required result tool. Return {schema_name} "
            "via its tool now, using the source_ref handles already received. "
            "Do not return prose or JSON as ordinary chat content. "
            "No additional investigation is needed to correct the response format."
        ))], "jump_to": "model"}

    async def awrap_model_call(self, request, handler):
        context = request.runtime.context
        await context.check_active()
        stalled = context.begin_round()
        if context.no_progress_closing_calls >= 2:
            raise NoProgressDetected("agent_no_progress")
        limits = context.provider.run.configuration.options.limits
        schema = request.response_format.schema
        tools = request.tools
        messages = context.compact_messages(request.messages)
        # Reserve the full packet, including all tools and room for budget guidance.
        estimate = request_input_tokens(
            ([request.system_message] if request.system_message else []) + messages, tools, schema) + 1024
        try:
            ticket, available = await context.budget.reserve(context.work, estimate, with_remaining=True)
        except BudgetExceeded as exc:
            logger.warning(
                "模型请求被预算阻止 review_id=%.8s work_id=%.8s attempt_id=%.8s reason=%s",
                context.provider.run.id, context.work.id, context.work.attempt_id, str(exc))
            raise
        context.model_calls += 1
        remaining = available["agent_model_calls"]
        call_remaining = min(remaining, available["model_calls"]) if available["model_calls"] is not None else remaining
        run = context.provider.run
        deadline = run.review_cutoff if context.work.kind == "review" else run.hard_deadline
        seconds = max(0, (deadline - utcnow()).total_seconds())
        closing_reasons = []
        if stalled >= 3 or context.no_progress_closing_calls:
            closing_reasons.append("no_progress")
        if (available["input_tokens"] is not None
                and available["input_tokens"] < 2 * estimate):
            closing_reasons.append("input_tokens")
        if (available["output_tokens"] is not None
                and available["output_tokens"] < 2 * limits.output_tokens):
            closing_reasons.append("output_tokens")
        if seconds <= settings.repoguardian_model_request_timeout_seconds:
            closing_reasons.append("time")
        if call_remaining <= 1:
            closing_reasons.append("call_fuse")
        if context.tool_calls >= limits.tool_calls:
            closing_reasons.append("tool_limit")
        if context.structured_output_retry:
            closing_reasons.append("format_correction")
        closing = bool(closing_reasons)
        context.investigation_closed = closing
        if "no_progress" in closing_reasons:
            context.no_progress_closing_calls += 1
        if closing:
            tools = [tool for tool in tools if tool.name == "submit_candidate"]
        if context.tool_calls >= limits.tool_calls:
            tools = []
        instruction = (
            f"\n\nShared input/output tokens available before this request, after protecting "
            f"verification and overview: {available['input_tokens'] if available['input_tokens'] is not None else 'unlimited'}/"
            f"{available['output_tokens'] if available['output_tokens'] is not None else 'unlimited'}. "
            f"Seconds to this role's cutoff: {seconds:.0f}. "
            f"Remaining model requests including this one: {remaining}. "
            f"Remaining Run model requests after protection: {available['model_calls'] if available['model_calls'] is not None else 'unlimited'}. "
            f"Remaining tool calls: {max(0, limits.tool_calls - context.tool_calls)}. "
            "Complete the necessary investigation; optional cost limits are not completion targets. "
            "Time and per-agent call/tool fuses still apply. "
            "All limits include structured output and correction requests. "
            "Finish as soon as the necessary investigation is complete; do not repeat "
            "reads or searches for already received evidence."
        )
        if stalled >= 2:
            instruction += (
                " The last two tool rounds returned no new information. Stop repeating those requests; "
                "use existing evidence, change the investigation, or finish with a concrete limitation."
            )
        if call_remaining == 2:
            instruction += (
                " The next request is reserved for your final structured result. "
                "Submit supported candidates now; use any remaining reads only for a critical missing premise."
            )
        if context.small_unit_ready_for_checkpoint() and not context.coverage_checkpoint_sent:
            context.coverage_checkpoint_sent = True
            instruction += (
                " Small-unit coverage checkpoint: all required change materials, related test source "
                "and consumer context have been received, together with a successful relationship search. "
                "These observations are not proof that every necessary question is answered. "
                "Return UnitCompletion now if the necessary checks are complete. Otherwise use the "
                "available tools only for a specific remaining premise; do not enumerate unrelated "
                "packages or transitive consumers. New relevant evidence may continue the investigation."
            )
        if closing:
            instruction += (
                f" Close now because of {','.join(closing_reasons)}. "
                f"No investigation tools are available. Return {schema.__name__} now using the "
                "evidence already received. If a necessary premise is still missing, "
                "state it concretely in unresolved_reason (Reviewer) or return insufficient "
                "(Verifier). Never present unfinished investigation as a clean result."
            )
            if tools:
                instruction += (
                    " If you have a newly supported claim, call submit_candidate AND "
                    "UnitCompletion in this same response so it is saved before this attempt ends."
                )
            if "no_progress" in closing_reasons:
                instruction += (
                    " Repetition alone is not a missing premise. Assess the evidence already received. "
                    "This is a bounded finalization opportunity, not permission to assume success."
                )
                instruction += (
                    " Finish with unresolved_reason=null if the necessary checks are complete; "
                    "otherwise identify the specific unanswered question."
                    if context.work.kind == "review" else
                    " Confirm or refute only with the required supporting evidence; "
                    "otherwise return insufficient and identify the missing premise."
                )
        guidance = HumanMessage(content="Runtime guidance (trusted host):" + instruction,
                                id=f"runtime-{ticket}")
        request = request.override(tools=tools, messages=[*messages, guidance])
        started = perf_counter()
        logger.info(
            "模型请求开始 review_id=%.8s work_id=%.8s kind=%s attempt_id=%.8s ticket=%.8s "
            "call=%d/%d estimated_input_tokens=%d remaining_input_tokens=%s remaining_output_tokens=%s "
            "remaining_seconds=%.0f closing=%s closing_reason=%s",
            context.provider.run.id, context.work.id, context.work.kind, context.work.attempt_id,
            ticket, context.model_calls, limits.model_calls, estimate, available["input_tokens"],
            available["output_tokens"], seconds, closing, ",".join(closing_reasons) or "-")
        pending, hunks = dict(context.pending), dict(context.pending_hunks)
        try:
            response = await handler(request)
        except asyncio.CancelledError:
            logger.info("模型请求取消 review_id=%.8s work_id=%.8s ticket=%.8s elapsed_ms=%.0f",
                        context.provider.run.id, context.work.id, ticket, (perf_counter() - started) * 1000)
            raise
        except Exception as exc:
            await context.budget.settle(ticket, None)
            logger.warning(
                "模型请求失败 review_id=%.8s work_id=%.8s ticket=%.8s elapsed_ms=%.0f error_type=%s usage=unknown",
                context.provider.run.id, context.work.id, ticket, (perf_counter() - started) * 1000,
                type(exc).__name__)
            raise
        message = next((item for item in reversed(response.result) if isinstance(item, AIMessage)), None)
        usage = getattr(message, "usage_metadata", None)
        await context.budget.settle(ticket, usage)
        called_tools = {call["name"] for call in message.tool_calls} if message else set()
        tool_names = [tool.name for tool in tools if tool.name in called_tools]
        if schema.__name__ in called_tools:
            tool_names.append(schema.__name__)
        logger.info(
            "模型请求返回 review_id=%.8s work_id=%.8s ticket=%.8s elapsed_ms=%.0f "
            "input_tokens=%s output_tokens=%s tool_calls=%d tools=%s",
            context.provider.run.id, context.work.id, ticket, (perf_counter() - started) * 1000,
            usage["input_tokens"] if usage is not None else "unknown",
            usage["output_tokens"] if usage is not None else "unknown",
            len(message.tool_calls) if message else 0, ",".join(tool_names) or "-")
        await context.check_active()
        # A successful response proves the model received the full request packet.
        await context.acknowledge(pending, hunks)
        # Keep the guidance at its original position in the append-only history.
        return ModelResponse(result=[guidance, *response.result],
                             structured_response=response.structured_response)


class AgentRunner:
    def __init__(self, model):
        self.model = model

    async def run(self, work, provider, budget):
        context = AgentContext(provider=provider, work=work, budget=budget)
        candidate = None
        started = perf_counter()
        logger.info("Agent开始 review_id=%.8s work_id=%.8s unit_id=%.8s kind=%s attempt=%d attempt_id=%.8s",
                    provider.run.id, work.id, work.unit_id, work.kind, work.attempt, work.attempt_id)
        try:
            role = "reviewer" if work.kind == "review" else "verifier"
            tools = list(READ_TOOLS)
            if role == "reviewer":
                tools.append(submit_candidate)
            if provider.run.configuration.options.include_pr_discussions and provider.discussions:
                tools.append(read_discussion)
            prompt = (Path(__file__).parent / "prompts" / f"{role}.md").read_text(encoding="utf-8")
            schema = UnitCompletion if role == "reviewer" else VerificationOutput
            if work.kind == "verification":
                candidate = next(item for item in await asyncio.to_thread(
                    provider.store.candidates, provider.run.id) if item.id == work.candidate_id)
            limits = provider.run.configuration.options.limits
            request_budget = max(1, min(unit_capacity(limits),
                                       limits.context_tokens - limits.output_tokens - 1024))
            if role == "verifier":
                usage = await asyncio.to_thread(provider.store.usage, provider.run.id)
                request_budget = verification_input_limit(limits, usage["committed_input_tokens"])
            initial_message = HumanMessage(content="", id=work.attempt_id)
            def estimate_initial(packet):
                return request_input_tokens([SystemMessage(content=prompt),
                    initial_message.model_copy(update={
                        "content": json.dumps(packet, ensure_ascii=False)})], tools, schema)
            initial = await provider.initial(context, candidate, request_budget=request_budget,
                                             estimate_request=estimate_initial)
            agent = create_agent(self.model, tools=tools, system_prompt=prompt,
                response_format=ToolStrategy(schema), middleware=[RequestGate()],
                context_schema=AgentContext, name=role)
            initial_message.content = json.dumps(initial, ensure_ascii=False)
            result = await agent.ainvoke(
                {"messages": [initial_message]},
                context=context, config={"recursion_limit": limits.model_calls * 8 + 24})
            output = result.get("structured_response")
            if not isinstance(output, schema):
                raise MissingStructuredResult
            await budget.active()
            if candidate:
                candidate = admit_verification(candidate, output, context)
            # ainvoke only returns after all tool tasks and their DB receipts finish.
            await asyncio.to_thread(provider.store.finish_work, provider.run.id, work,
                "completed", output.model_dump(), None, provider.lease,
                candidate=candidate, tool_calls=context.tool_calls)
            logger.info(
                "Agent结果已提交 review_id=%.8s work_id=%.8s kind=%s elapsed_ms=%.0f "
                "model_calls=%d tool_calls=%d verdict=%s finding=%s unresolved=%s",
                provider.run.id, work.id, work.kind, (perf_counter() - started) * 1000,
                context.model_calls, context.tool_calls,
                candidate.verification.verdict if candidate else "-",
                candidate.finding is not None if candidate else False,
                bool(output.unresolved_reason) if isinstance(output, UnitCompletion) else False)
        except (LeaseLost, ReviewStopped, asyncio.CancelledError) as exc:
            logger.info("Agent停止 review_id=%.8s work_id=%.8s reason=%s",
                        provider.run.id, work.id, type(exc).__name__)
            raise
        except Exception as exc:
            if isinstance(exc, NoProgressDetected):
                code = "agent_no_progress"
            elif isinstance(exc, BudgetExceeded):
                code = str(exc)
            elif isinstance(exc, MissingStructuredResult):
                code = "agent_structured_output_missing"
            else:
                code = "agent_execution_failed"
            await asyncio.to_thread(provider.store.finish_work, provider.run.id, work,
                "interrupted" if isinstance(exc, (BudgetExceeded, NoProgressDetected)) else "failed", None, code,
                provider.lease, tool_calls=context.tool_calls)
            logger.warning(
                "Agent未完成 review_id=%.8s work_id=%.8s kind=%s code=%s error_type=%s "
                "model_calls=%d tool_calls=%d elapsed_ms=%.0f",
                provider.run.id, work.id, work.kind, code, type(exc).__name__,
                context.model_calls, context.tool_calls, (perf_counter() - started) * 1000)
