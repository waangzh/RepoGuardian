import asyncio
import json
import logging
import time
from dataclasses import replace

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.callbacks import AsyncCallbackHandler
from langsmith import Client, tracing_context

from app.review.agents import agent_recursion_limit, build_agent
from app.review.models import (
    AgentAttempt, AgentResult, BatchVerificationOutput, DiscoveryOutput, LimitationReviewOutput, VerificationOutput,
)
from app.review.limitations import LIMITATION_CALL_LIMITS, validate_assessments
from app.review.tools import REVIEW_TOOLS
from app.review.materials import prepare_materials

logger = logging.getLogger("RepoGuardian.Review")
SOURCE_TOOLS = {item.name for item in REVIEW_TOOLS}


class AgentUsage(AsyncCallbackHandler):
    """Observation only; framework middleware still owns call limits."""

    def __init__(self, review_id="", identity="", reviewer=""):
        self.started = set()
        self.settled = set()
        self.input_tokens = 0
        self.output_tokens = 0
        self.unknown = False
        self.review_id, self.identity, self.reviewer = review_id, identity, reviewer
        self.tools_started, self.tools_settled = {}, set()
        self.evidence, self.pending_pages, self.consumed_pages = set(), set(), set()
        self.tool_failures = 0
        self.tool_calls = 0
        self.quiet = False
        self.policy_exclusions = self.incomplete_searches = self.structured_output_retries = 0
        self.cached_input_tokens = 0
        self.cache_known = True
        self.structured_errors = set()

    async def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        self.started.add(run_id)
        logger.debug("模型调用 review_id=%s agent_id=%s reviewer=%s call=%s",
                     self.review_id, self.identity, self.reviewer, len(self.started))

    async def on_llm_error(self, error, *, run_id, **kwargs):
        self.unknown = True

    async def on_chain_end(self, outputs, **kwargs):
        if isinstance(outputs, dict):
            for message in outputs.get("messages", []):
                if (isinstance(message, ToolMessage) and message.name in {
                        "DiscoveryOutput", "VerificationOutput", "BatchVerificationOutput", "LimitationReviewOutput"}
                        and str(message.content).startswith("Error:")):
                    self.structured_errors.add(message.tool_call_id)
            self.structured_output_retries = len(self.structured_errors)

    async def on_llm_end(self, response, *, run_id, **kwargs):
        if run_id in self.settled:
            return
        self.settled.add(run_id)
        generation = response.generations[0][0] if response.generations and response.generations[0] else None
        message = getattr(generation, "message", None)
        self.tool_calls += len(getattr(message, "tool_calls", []))
        final_schemas = {schema.__name__: schema for schema in (
            DiscoveryOutput, VerificationOutput, BatchVerificationOutput, LimitationReviewOutput)}
        finals = [call for call in getattr(message, "tool_calls", []) if call["name"] in final_schemas]
        for call in finals:
            try:
                final_schemas[call["name"]].model_validate(call["args"])
                if len(finals) > 1:
                    self.structured_errors.add(call["id"])
            except ValueError:
                self.structured_errors.add(call["id"])
        self.structured_output_retries = len(self.structured_errors)
        usage = getattr(message, "usage_metadata", None)
        if usage is None:
            self.unknown = True
        else:
            self.input_tokens += usage["input_tokens"]
            self.output_tokens += usage["output_tokens"]
            cached = usage.get("input_token_details", {}).get("cache_read")
            if cached is None:
                self.cache_known = False
            else:
                self.cached_input_tokens += cached

    async def on_tool_start(self, serialized, input_str, *, run_id, **kwargs):
        name = (serialized or {}).get("name")
        if name in SOURCE_TOOLS:
            self.tools_started[run_id] = name

    async def on_tool_end(self, output, *, run_id, **kwargs):
        if run_id not in self.tools_started or run_id in self.tools_settled:
            return
        self.tools_settled.add(run_id)
        name = self.tools_started[run_id]
        try:
            body = output.content if isinstance(output, ToolMessage) else output
            body = json.loads(body) if isinstance(body, str) else body
            if not isinstance(body, dict):
                raise ValueError("invalid_tool_response")
            if body.get("ok") is not True or body.get("truncated"):
                self.tool_failures += 1
                code = body.get("error")
                # Only first-party codes are logged; never provider text or tool arguments.
                code = code if isinstance(code, str) and code.isidentifier() and len(code) <= 80 else "tool_failed"
                if not self.quiet:
                    logger.warning("工具拒绝 review_id=%s agent_id=%s reviewer=%s tool=%s code=%s",
                        self.review_id, self.identity, self.reviewer, name, code)
            if body.get("ok"):
                self.policy_exclusions += body.get("policy_exclusions", 0)
                if body.get("scope_complete") is False or body.get("unreadable_files"):
                    self.incomplete_searches += 1
                    if not self.quiet:
                        logger.info("搜索范围诊断 review_id=%s agent_id=%s tool=%s has_more=%s unreadable=%s excluded=%s",
                            self.review_id, self.identity, name, body.get("has_more", False),
                            body.get("unreadable_files", 0), body.get("policy_exclusions", 0))
            if body.get("ok") and body.get("pagination_id"):
                pagination = body["pagination_id"]
                if body.get("cursor"):
                    self.consumed_pages.add((pagination, body["cursor"]))
                if body.get("next_cursor"):
                    self.pending_pages.add((pagination, body["next_cursor"]))
            if name in {"read_file", "read_base_file", "read_symbol"} and body.get("ok") and body.get("evidence"):
                self.evidence.add(body["evidence"]["id"])
        except (ValueError, TypeError, KeyError):
            self.tool_failures += 1

    async def on_tool_error(self, error, *, run_id, **kwargs):
        if run_id in self.tools_started and run_id not in self.tools_settled:
            self.tools_settled.add(run_id)
            self.tool_failures += 1

    def values(self):
        known = not self.unknown and self.started == self.settled
        return {"model_calls": len(self.started),
                "input_tokens": self.input_tokens if known else None,
                "output_tokens": self.output_tokens if known else None,
                "tool_calls": self.tool_calls,
                "tool_failures": self.tool_failures,
                "evidence_ids": sorted(self.evidence),
                "unconsumed_pages": len(self.pending_pages - self.consumed_pages)}

    async def reconcile(self, messages):
        # Restored subgraphs may contain reads made before this callback existed.
        self.quiet = True
        self.tools_started, self.tools_settled = {}, set()
        self.evidence, self.pending_pages, self.consumed_pages = set(), set(), set()
        self.tool_failures = 0
        self.tool_calls = sum(len(message.tool_calls) for message in messages if isinstance(message, AIMessage))
        self.policy_exclusions = self.incomplete_searches = 0
        for message in messages:
            if (isinstance(message, ToolMessage) and message.name in {
                    "DiscoveryOutput", "VerificationOutput", "BatchVerificationOutput", "LimitationReviewOutput"}
                    and str(message.content).startswith("Error:")):
                self.structured_errors.add(message.tool_call_id)
            if isinstance(message, ToolMessage) and message.name in SOURCE_TOOLS:
                self.tools_started[message.tool_call_id] = message.name
                await self.on_tool_end(message, run_id=message.tool_call_id)
        self.structured_output_retries = len(self.structured_errors)



async def run_agent(context, identity, reviewer, payload, *, discovery_agent_ids=()):
    sources = replace(await context.sources(), budget=context.budget, budget_agent_id=identity,
                      discovery_agent_ids=frozenset(discovery_agent_ids),
                      goals=tuple(payload.get("goals", [])),
                      review_limitations=tuple(payload.get("review_limitations", [])))
    payload = {**payload, "snapshot": sources.evidence.snapshot.model_dump(),
               "author_context": sources.author_context,
               "discussions": [{"index": index, "id": item.get("id"), "path": item.get("path"),
                    "source": item.get("source")} for index, item in enumerate(sources.discussions)]}
    started = time.monotonic()
    previous = context.retry_results.get((reviewer, identity))
    if previous is None:
        previous = next((item for (role, _), item in context.retry_results.items()
                         if role == reviewer and item.get("execution_id") == identity), None)
    limits = context.limits.model_copy(deep=True)
    if reviewer == "limitation_reviewer":
        limits.model_calls = min(limits.model_calls, LIMITATION_CALL_LIMITS[context.depth])
        limits.tool_calls = min(limits.tool_calls, 24)
    model_call_limit, tool_call_limit = limits.model_calls, limits.tool_calls
    if previous:
        limits.model_calls -= previous["model_calls"]
        limits.tool_calls -= previous["tool_calls"]
    await context.emit(phase=reviewer, agent_id=identity, status="running",
                       model_call_limit=model_call_limit, tool_call_limit=tool_call_limit)
    usage_observer = AgentUsage(sources.evidence.snapshot.review_id, identity, reviewer)
    logger.info("审查分支开始 review_id=%s agent_id=%s reviewer=%s model_limit=%s tool_limit=%s",
        sources.evidence.snapshot.review_id, identity, reviewer, limits.model_calls, limits.tool_calls)
    output, error = {}, None
    material_ids = set()
    try:
        if limits.model_calls <= 0 or limits.tool_calls <= 0:
            raise ValueError("agent_budget_exhausted")
        materials, material_ids = await prepare_materials(sources, payload, context.depth)
        sources.material_evidence_ids = frozenset(material_ids)
        payload = {**payload, **materials}
        agent = await asyncio.to_thread(build_agent, context.model, reviewer, limits,
                                       batch="candidates" in payload)
        from app.core.config import settings
        client = (await asyncio.to_thread(Client, api_key=settings.langsmith_api_key, api_url=settings.langsmith_endpoint)
                  if settings.repoguardian_langsmith_tracing else None)
        with tracing_context(enabled=settings.repoguardian_langsmith_tracing, client=client,
                             project_name=settings.langsmith_project):
            result = await agent.ainvoke(
                {"messages": [("user", json.dumps(payload, ensure_ascii=False))]},
                context=sources, config={"recursion_limit": agent_recursion_limit(limits),
                                         "callbacks": [usage_observer]})
        await context.active()
        await usage_observer.reconcile(result["messages"])
        output = result["structured_response"].model_dump()
        if reviewer == "limitation_reviewer":
            await asyncio.to_thread(validate_assessments, LimitationReviewOutput.model_validate(output),
                sources.review_limitations, sources.goals, usage_observer.evidence | material_ids, sources.evidence)
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise
    except Exception as exc:
        await context.active()
        error = type(exc).__name__
        logger.error("审查分支失败 review_id=%s agent_id=%s reviewer=%s code=%s model_calls=%s tool_calls=%s",
            sources.evidence.snapshot.review_id, identity, reviewer, error,
            len(usage_observer.started), len(usage_observer.tools_started))
    if context.budget is not None:
        await context.budget.finish(identity)
    envelope = AgentResult(id=identity, status="failed" if error else "completed", error=error,
        output=output, investigation_limitations=output.get("limitations", []), **usage_observer.values())
    envelope.evidence_ids = sorted(set(envelope.evidence_ids) | material_ids)
    envelope.policy_exclusions = usage_observer.policy_exclusions
    envelope.incomplete_searches = usage_observer.incomplete_searches
    envelope.structured_output_retries = usage_observer.structured_output_retries
    envelope.cached_input_tokens = (usage_observer.cached_input_tokens
        if not usage_observer.unknown and usage_observer.cache_known else None)
    node = ("review_limitation" if reviewer == "limitation_reviewer" else
            "verify" if reviewer == "verifier" else "investigate" if reviewer == "investigator" else "discover")
    if node in context.replaying_nodes:
        # A wrapper node may restart read-only work after interruption. Prior
        # transport completion is unknown; do not label only new calls as total.
        envelope.model_calls = envelope.input_tokens = envelope.output_tokens = envelope.tool_calls = None
        envelope.cached_input_tokens = None
    if previous:
        envelope.previous_attempts = [AgentAttempt.model_validate(
            {key: value for key, value in previous.items() if key in AgentAttempt.model_fields})]
        for key in ("model_calls", "input_tokens", "output_tokens", "tool_calls"):
            old, new = previous.get(key), getattr(envelope, key)
            setattr(envelope, key, old + new if old is not None and new is not None else None)
        envelope.tool_failures += previous["tool_failures"]
        # Prior reads remain auditable, but a fresh attempt may only cite its own reads.
        envelope.unconsumed_pages += previous["unconsumed_pages"]
        for key in ("policy_exclusions", "incomplete_searches", "structured_output_retries"):
            setattr(envelope, key, getattr(envelope, key) + previous.get(key, 0))
        old_cached = previous.get("cached_input_tokens")
        envelope.cached_input_tokens = (old_cached + envelope.cached_input_tokens
            if old_cached is not None and envelope.cached_input_tokens is not None else None)
    elapsed = round(time.monotonic() - started, 3)
    logger.info("审查分支结束 review_id=%s agent_id=%s reviewer=%s status=%s elapsed=%s model_calls=%s "
        "tool_calls=%s tool_failures=%s input_tokens=%s output_tokens=%s",
        sources.evidence.snapshot.review_id, identity, reviewer, envelope.status, elapsed,
        envelope.model_calls, envelope.tool_calls, envelope.tool_failures, envelope.input_tokens, envelope.output_tokens)
    await context.emit(phase=reviewer, agent_id=identity, status=envelope.status,
        error=envelope.error, model_calls=envelope.model_calls, tool_calls=envelope.tool_calls,
        tool_failures=envelope.tool_failures, elapsed_seconds=elapsed,
        policy_exclusions=envelope.policy_exclusions, incomplete_searches=envelope.incomplete_searches,
        structured_output_retries=envelope.structured_output_retries,
        model_call_limit=model_call_limit, tool_call_limit=tool_call_limit)
    return envelope.model_dump()
