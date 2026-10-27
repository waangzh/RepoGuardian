"""One production graph: native fan-out, evidence gate, independent verification."""

import asyncio
import json

from langchain_core.messages import ToolMessage
from langchain_core.callbacks import AsyncCallbackHandler
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send
from langsmith import Client, tracing_context

from app.review.agents import build_agent
from app.review.context import ReviewContext
from app.review.findings import (
    aggregate_findings, candidate_gate, collect_candidates, comparison_candidates,
    covered_goals, verify_finding,
)
from app.review.models import AgentResult, CandidateFinding, ReviewReport, VerificationResult
from app.review.planning import plan_review
from app.review.state import ReviewState


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


async def prepare(state: ReviewState, runtime: Runtime[ReviewContext]):
    sources = await runtime.context.sources()
    if state["review_id"] != sources.evidence.snapshot.review_id:
        raise ValueError("review_snapshot_identity_mismatch")
    await runtime.context.emit(phase="prepare", status="completed")
    return {"snapshot": sources.evidence.snapshot.model_dump(), "status": "planning"}


async def plan(state: ReviewState, runtime: Runtime[ReviewContext]):
    context = runtime.context
    sources = await context.sources()
    goals, assignments, _ = plan_review(
        sources.changed_files, depth=context.depth, selected_paths=context.selected_paths, focus=context.focus,
        repository_graph=sources.repository_graph, symbols=sources.symbols)
    await context.emit(phase="planning", assignments=[item.model_dump() for item in assignments])
    return {"goals": [item.model_dump() for item in goals],
            "assignments": [item.model_dump() for item in assignments], "status": "discovering"}


def route_discovery(state):
    return [Send("discover", {"review_id": state["review_id"], "assignment": item,
                             "goals": [goal for goal in state["goals"] if goal["id"] in item["goal_ids"]]})
            for item in state["assignments"]] or "collect"


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
        evidence, read_errors, pending_pages, consumed_pages = set(), 0, set(), set()
        for message in result["messages"]:
            if isinstance(message, ToolMessage) and message.name in {
                "read_file", "read_base_file", "read_diff", "read_symbol", "search_code",
                "find_files", "find_references", "get_callers", "get_callees", "read_discussion",
            }:
                try:
                    body = json.loads(message.content)
                    if body.get("ok") is not True or body.get("truncated") or body.get("unreadable_files"):
                        read_errors += 1
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
                    read_errors += 1
        read_errors += len(pending_pages - consumed_pages)
        output = result["structured_response"].model_dump()
        envelope = AgentResult(id=identity, status="completed", output=output,
            evidence_ids=sorted(evidence), read_errors=read_errors, **usage_observer.values())
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


async def discover(state: ReviewState, runtime: Runtime[ReviewContext]):
    assignment = state["assignment"]
    result = await run_agent(runtime.context, assignment["id"], assignment["reviewer"],
                             {"assignment": assignment, "goals": state["goals"]})
    return {"discovery_results": [result]}


async def collect(state: ReviewState, runtime: Runtime[ReviewContext]):
    await runtime.context.active()
    candidates = collect_candidates(state.get("discovery_results", []))
    await runtime.context.emit(phase="collect", candidates=len(candidates))
    return {"candidates": candidates}


async def evidence_gate(state: ReviewState, runtime: Runtime[ReviewContext]):
    sources = await runtime.context.sources()
    changes = [item for item in sources.changed_files if runtime.context.selected_paths is None
               or item["file_path"] in runtime.context.selected_paths]
    eligible, rejections = [], []
    for raw in state["candidates"]:
        candidate = CandidateFinding.model_validate(raw)
        try:
            await asyncio.to_thread(candidate_gate, candidate, sources.evidence,
                                    state.get("discovery_results", []), changes)
            eligible.append(raw)
        except (ValueError, OSError):
            rejections.append(VerificationResult(candidate_id=candidate.id,
                decision="insufficient_evidence", explanation="候选证据未通过源码与快照校验",
                attribution="uncertain").model_dump())
    return {"eligible_candidates": eligible, "gate_rejections": rejections, "status": "verifying"}


def route_verification(state):
    return [Send("verify", {"review_id": state["review_id"], "candidate": item,
                           "related_candidates": comparison_candidates(item, state["eligible_candidates"])})
            for item in state["eligible_candidates"]] or "arbitrate"


async def verify(state: ReviewState, runtime: Runtime[ReviewContext]):
    candidate = state["candidate"]
    result = await run_agent(runtime.context, candidate["id"], "verifier",
        {"candidate": candidate, "related_candidates": state.get("related_candidates", [])})
    return {"verification_results": [result]}


async def arbitrate(state: ReviewState, runtime: Runtime[ReviewContext]):
    sources = await runtime.context.sources()
    by_id = {item["id"]: item for item in state.get("verification_results", [])}
    by_id.update({item["id"]: item for item in state.get("investigation_results", [])
                  if item["status"] == "completed"})
    findings, decisions = [], list(state["gate_rejections"])
    for raw in state["eligible_candidates"]:
        candidate = CandidateFinding.model_validate(raw)
        result = by_id.get(candidate.id)
        try:
            if result is None or result["status"] != "completed":
                raise ValueError("verifier_failed")
            decision, finding = await asyncio.to_thread(
                verify_finding, candidate, result, sources.evidence, sources.changed_files,
                comparison_candidates(raw, state["eligible_candidates"]))
            decisions.append(decision.model_dump())
            if finding:
                findings.append(finding)
        except (ValueError, OSError):
            decisions.append(VerificationResult(candidate_id=candidate.id,
                decision="insufficient_evidence", explanation="独立核验未完成或源码证据不足",
                attribution="uncertain").model_dump())
    await runtime.context.emit(phase="arbitrate", confirmed=len(findings),
                               unresolved=sum(item["decision"] == "insufficient_evidence" for item in decisions))
    return {"findings": [item.model_dump() for item in aggregate_findings(findings)], "decisions": decisions}


def route_investigation(state):
    if state.get("investigation_round", 0):
        return "report"
    pending = {item["candidate_id"] for item in state["decisions"]
               if item["decision"] == "insufficient_evidence"}
    eligible = {item["id"] for item in state["eligible_candidates"]}
    candidates = [item for item in state["candidates"]
                  if item["id"] in pending & eligible and item["severity"] in {"critical", "high"}]
    return [Send("investigate", {"review_id": state["review_id"], "candidate": item,
                                "related_candidates": comparison_candidates(item, state["eligible_candidates"])})
            for item in candidates] or "report"


async def investigate(state: ReviewState, runtime: Runtime[ReviewContext]):
    candidate = state["candidate"]
    result = await run_agent(runtime.context, candidate["id"], "investigator",
        {"candidate": candidate, "related_candidates": state.get("related_candidates", [])})
    return {"investigation_results": [result]}


async def finish_investigation(state: ReviewState, runtime: Runtime[ReviewContext]):
    await runtime.context.active()
    return {"investigation_round": 1}


async def report(state: ReviewState, runtime: Runtime[ReviewContext]):
    sources = await runtime.context.sources()
    goals = await asyncio.to_thread(covered_goals, state["goals"], state.get("discovery_results", []),
                                    sources.evidence, state["assignments"])
    results = state.get("discovery_results", [])
    complete = bool(goals) and all(goal["status"] == "examined" for goal in goals)
    complete = complete and len(results) == len(state["assignments"])
    complete = complete and all(item["status"] == "completed" for item in results)
    unresolved = any(item["decision"] == "insufficient_evidence" for item in state["decisions"])
    unresolved = unresolved or len(state["decisions"]) != len(state["candidates"])
    verifier_failed = any(item["status"] != "completed"
        for item in state.get("verification_results", []) + state.get("investigation_results", []))
    limitations = any(item.get("read_errors") or item.get("output", {}).get("limitations")
        for item in results + state.get("verification_results", []) + state.get("investigation_results", []))
    termination = "completed" if complete and not unresolved and not verifier_failed and not limitations else "partial"
    output = ReviewReport(snapshot=state["snapshot"], assignments=state["assignments"],
        goals=goals, discovery_results=results, verification_results=state.get("verification_results", [])
        + state.get("investigation_results", []), candidates=state["candidates"],
        decisions=state["decisions"], findings=state["findings"], complete=complete,
        termination=termination)
    await runtime.context.emit(phase="report", status="completed", termination=termination)
    return {"report": output.model_dump(), "goals": goals, "status": termination}


def build_review_graph(checkpointer=None):
    graph = StateGraph(ReviewState, context_schema=ReviewContext)
    for name, node in (
        ("prepare", prepare), ("plan", plan), ("discover", discover), ("collect", collect),
        ("evidence_gate", evidence_gate), ("verify", verify), ("arbitrate", arbitrate),
        ("investigate", investigate), ("finish_investigation", finish_investigation), ("report", report),
    ):
        graph.add_node(name, node)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "plan")
    graph.add_conditional_edges("plan", route_discovery, ["discover", "collect"])
    graph.add_edge("discover", "collect")
    graph.add_edge("collect", "evidence_gate")
    graph.add_conditional_edges("evidence_gate", route_verification, ["verify", "arbitrate"])
    graph.add_edge("verify", "arbitrate")
    graph.add_conditional_edges("arbitrate", route_investigation, ["investigate", "report"])
    graph.add_edge("investigate", "finish_investigation")
    graph.add_edge("finish_investigation", "arbitrate")
    graph.add_edge("report", END)
    return graph.compile(checkpointer=checkpointer)
