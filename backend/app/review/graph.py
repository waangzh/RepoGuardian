"""One production graph: native fan-out, evidence gate, independent verification."""

import asyncio
import json

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send

from app.review.agents import build_agent
from app.review.context import ReviewContext
from app.review.findings import (
    candidate_gate, collect_candidates, covered_goals, verify_finding,
)
from app.review.models import AgentResult, CandidateFinding, ReviewReport, VerificationResult
from app.review.planning import plan_review
from app.review.state import ReviewState


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
        sources.changed_files, depth=context.depth, selected_paths=context.selected_paths)
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
    try:
        agent = await asyncio.to_thread(build_agent, context.model, reviewer, context.limits)
        result = await agent.ainvoke(
            {"messages": [("user", json.dumps(payload, ensure_ascii=False))]},
            context=sources, config={"recursion_limit": context.limits.model_calls * 4 + 8})
        await context.active()
        evidence, calls, input_tokens, output_tokens, usage_known = set(), 0, 0, 0, True
        for message in result["messages"]:
            if isinstance(message, ToolMessage) and message.name in {"read_file", "read_base_file", "read_symbol"}:
                try:
                    body = json.loads(message.content)
                    if body.get("ok") and body.get("evidence"):
                        evidence.add(body["evidence"]["id"])
                except (ValueError, TypeError, KeyError):
                    pass
            if isinstance(message, AIMessage):
                calls += 1
                usage = message.usage_metadata
                if usage is None:
                    usage_known = False
                else:
                    input_tokens += usage["input_tokens"]
                    output_tokens += usage["output_tokens"]
        output = result["structured_response"].model_dump()
        envelope = AgentResult(id=identity, status="completed", output=output,
            evidence_ids=sorted(evidence), model_calls=calls,
            input_tokens=input_tokens if usage_known else None,
            output_tokens=output_tokens if usage_known else None)
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise
    except Exception as exc:
        await context.active()
        envelope = AgentResult(id=identity, status="failed", error=type(exc).__name__)
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
    eligible, rejections = [], []
    for raw in state["candidates"]:
        candidate = CandidateFinding.model_validate(raw)
        try:
            await asyncio.to_thread(candidate_gate, candidate, sources.evidence,
                                    state.get("discovery_results", []), sources.changed_files)
            eligible.append(raw)
        except (ValueError, OSError):
            rejections.append(VerificationResult(candidate_id=candidate.id,
                decision="insufficient_evidence", explanation="候选证据未通过源码与快照校验",
                attribution="uncertain").model_dump())
    return {"eligible_candidates": eligible, "gate_rejections": rejections, "status": "verifying"}


def route_verification(state):
    return [Send("verify", {"review_id": state["review_id"], "candidate": item})
            for item in state["eligible_candidates"]] or "arbitrate"


async def verify(state: ReviewState, runtime: Runtime[ReviewContext]):
    candidate = state["candidate"]
    result = await run_agent(runtime.context, candidate["id"], "verifier", {"candidate": candidate})
    return {"verification_results": [result]}


async def arbitrate(state: ReviewState, runtime: Runtime[ReviewContext]):
    sources = await runtime.context.sources()
    by_id = {item["id"]: item for item in state.get("verification_results", [])}
    by_id.update({item["id"]: item for item in state.get("investigation_results", [])})
    findings, decisions = [], list(state["gate_rejections"])
    for raw in state["eligible_candidates"]:
        candidate = CandidateFinding.model_validate(raw)
        result = by_id.get(candidate.id)
        try:
            if result is None or result["status"] != "completed":
                raise ValueError("verifier_failed")
            decision, finding = await asyncio.to_thread(
                verify_finding, candidate, result, sources.evidence, sources.changed_files)
            decisions.append(decision.model_dump())
            if finding:
                findings.append(finding.model_dump())
        except (ValueError, OSError):
            decisions.append(VerificationResult(candidate_id=candidate.id,
                decision="insufficient_evidence", explanation="独立核验未完成或源码证据不足",
                attribution="uncertain").model_dump())
    await runtime.context.emit(phase="arbitrate", confirmed=len(findings),
                               unresolved=sum(item["decision"] == "insufficient_evidence" for item in decisions))
    return {"findings": findings, "decisions": decisions}


def route_investigation(state):
    if state.get("investigation_round", 0):
        return "report"
    pending = {item["candidate_id"] for item in state["decisions"]
               if item["decision"] == "insufficient_evidence"}
    eligible = {item["id"] for item in state["eligible_candidates"]}
    candidates = [item for item in state["candidates"]
                  if item["id"] in pending & eligible and item["severity"] in {"critical", "high"}]
    return [Send("investigate", {"review_id": state["review_id"], "candidate": item})
            for item in candidates] or "report"


async def investigate(state: ReviewState, runtime: Runtime[ReviewContext]):
    candidate = state["candidate"]
    result = await run_agent(runtime.context, candidate["id"], "investigator", {"candidate": candidate})
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
    complete = complete and all(item["status"] == "completed" for item in results)
    unresolved = any(item["decision"] == "insufficient_evidence" for item in state["decisions"])
    termination = "completed" if complete and not unresolved else "partial"
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
