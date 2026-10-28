import asyncio

from langgraph.runtime import Runtime

from app.review.context import ReviewContext
from app.review.findings import collect_candidates
from app.review.limitations import limitation_batches, limitation_discoveries, review_limitations
from app.review.nodes.agent_execution import run_agent
from app.review.state import ReviewState


async def plan_limitations(state: ReviewState, runtime: Runtime[ReviewContext]):
    if state.get("limitation_round"):
        return {}
    sources = await runtime.context.sources()
    records = await asyncio.to_thread(review_limitations, state, sources.evidence)
    return {"limitation_batches": limitation_batches(records, state["goals"])}


async def review_limitation(state: ReviewState, runtime: Runtime[ReviewContext]):
    payload = {"review_limitations": state["review_limitations"], "goals": state["goals"]}
    result = await run_agent(runtime.context, state["limitation_batch_id"], "limitation_reviewer", payload)
    return {"limitation_results": [result]}


async def collect_limitations(state: ReviewState, runtime: Runtime[ReviewContext]):
    await runtime.context.active()
    results = state.get("discovery_results", []) + limitation_discoveries(state.get("limitation_results", []))
    candidates = collect_candidates(results)
    added = {item["id"] for item in candidates} - {item["id"] for item in state["candidates"]}
    return {"candidates": candidates, "limitation_round": 1, "limitation_new_candidates": bool(added)}
