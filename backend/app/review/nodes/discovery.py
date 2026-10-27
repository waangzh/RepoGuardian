from langgraph.runtime import Runtime

from app.review.context import ReviewContext
from app.review.state import ReviewState
from app.review.findings import collect_candidates
from app.review.nodes.agent_execution import run_agent


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
