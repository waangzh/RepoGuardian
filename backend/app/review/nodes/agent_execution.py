import asyncio
import json

from langchain_core.messages import ToolMessage
from langchain_core.callbacks import AsyncCallbackHandler
from langsmith import Client, tracing_context

from app.review.agents import build_agent
from app.review.models import AgentResult


class AgentUsage(AsyncCallbackHandler):
    """Observation only; framework middleware still owns call limits."""

    def __init__(self):
        self.started = set()
        self.settled = set()
        self.input_tokens = 0
        self.output_tokens = 0
        self.unknown = False

    async def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        self.started.add(run_id)

    async def on_llm_end(self, response, *, run_id, **kwargs):
        if run_id in self.settled:
            return
        self.settled.add(run_id)
        generation = response.generations[0][0] if response.generations and response.generations[0] else None
        usage = getattr(getattr(generation, "message", None), "usage_metadata", None)
        if usage is None:
            self.unknown = True
        else:
            self.input_tokens += usage["input_tokens"]
            self.output_tokens += usage["output_tokens"]

    def values(self):
        known = not self.unknown and self.started == self.settled
        return {"model_calls": len(self.started),
                "input_tokens": self.input_tokens if known else None,
                "output_tokens": self.output_tokens if known else None}



async def run_agent(context, identity, reviewer, payload):
    sources = await context.sources()
    await context.emit(phase=reviewer, agent_id=identity, status="running")
    payload = {**payload, "snapshot": sources.evidence.snapshot.model_dump(),
               "author_context": sources.author_context,
               "discussions": [{"index": index, "id": item.get("id"), "path": item.get("path"),
                    "source": item.get("source")} for index, item in enumerate(sources.discussions)]}
    usage_observer = AgentUsage()
    try:
        agent = await asyncio.to_thread(build_agent, context.model, reviewer, context.limits)
        from app.core.config import settings
        client = (await asyncio.to_thread(Client, api_key=settings.langsmith_api_key, api_url=settings.langsmith_endpoint)
                  if settings.repoguardian_langsmith_tracing else None)
        with tracing_context(enabled=settings.repoguardian_langsmith_tracing, client=client,
                             project_name=settings.langsmith_project):
            result = await agent.ainvoke(
                {"messages": [("user", json.dumps(payload, ensure_ascii=False))]},
                context=sources, config={"recursion_limit": context.limits.model_calls * 4 + 8,
                                         "callbacks": [usage_observer]})
        await context.active()
        evidence, tool_failures, pending_pages, consumed_pages = set(), 0, set(), set()
        for message in result["messages"]:
            if isinstance(message, ToolMessage) and message.name in {
                "read_file", "read_base_file", "read_diff", "read_symbol", "search_code",
                "find_files", "find_references", "get_callers", "get_callees", "read_discussion",
            }:
                try:
                    body = json.loads(message.content)
                    if not isinstance(body, dict):
                        raise ValueError("invalid_tool_response")
                    if body.get("ok") is not True or body.get("truncated") or body.get("unreadable_files"):
                        tool_failures += 1
                    if body.get("ok") and body.get("pagination_id"):
                        pagination = body["pagination_id"]
                        if body.get("cursor"):
                            consumed_pages.add((pagination, body["cursor"]))
                        if body.get("next_cursor"):
                            pending_pages.add((pagination, body["next_cursor"]))
                    if (message.name in {"read_file", "read_base_file", "read_symbol"}
                            and body.get("ok") and body.get("evidence")):
                        evidence.add(body["evidence"]["id"])
                except (ValueError, TypeError, KeyError):
                    tool_failures += 1
        output = result["structured_response"].model_dump()
        investigation_limitations = output.get("limitations", [])
        envelope = AgentResult(id=identity, status="completed", output=output,
            evidence_ids=sorted(evidence), tool_failures=tool_failures,
            unconsumed_pages=len(pending_pages - consumed_pages),
            investigation_limitations=investigation_limitations, **usage_observer.values())
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise
    except Exception as exc:
        await context.active()
        envelope = AgentResult(id=identity, status="failed", error=type(exc).__name__,
                               **usage_observer.values())
    node = "verify" if reviewer == "verifier" else "investigate" if reviewer == "investigator" else "discover"
    if node in context.replaying_nodes:
        # A wrapper node may restart read-only work after interruption. Prior
        # transport completion is unknown; do not label only new calls as total.
        envelope.model_calls = envelope.input_tokens = envelope.output_tokens = None
    await context.emit(phase=reviewer, agent_id=identity, status=envelope.status,
                       error=envelope.error)
    return envelope.model_dump()
