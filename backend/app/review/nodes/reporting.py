import asyncio

from langgraph.runtime import Runtime

from app.review.context import ReviewContext
from app.review.state import ReviewState
from app.review.findings import covered_goals
from app.review.models import ReviewReport


async def report(state: ReviewState, runtime: Runtime[ReviewContext]):
    sources = await runtime.context.sources()
    goals = await asyncio.to_thread(covered_goals, state["goals"], state.get("discovery_results", []),
                                    sources.evidence, state["assignments"])
    results = state.get("discovery_results", [])
    complete = bool(goals) and all(goal["status"] == "examined" for goal in goals)
    verification = state.get("verification_results", [])
    investigations = state.get("investigation_results", [])
    discovery_complete = ({item["id"] for item in results}
                          == {item["id"] for item in state["assignments"]})
    verification_complete = ({item["id"] for item in verification}
                             == {item["id"] for item in state["eligible_candidates"]})
    execution_complete = discovery_complete and verification_complete and all(
        item["status"] == "completed" for item in results + verification + investigations)
    termination = "completed" if execution_complete else "partial"
    output = ReviewReport(snapshot=state["snapshot"], assignments=state["assignments"],
        goals=goals, discovery_results=results, verification_results=verification + investigations,
        candidates=state["candidates"],
        decisions=state["decisions"], findings=state["findings"], complete=complete,
        termination=termination)
    await runtime.context.emit(phase="report", status="completed", termination=termination)
    return {"report": output.model_dump(), "goals": goals, "status": termination}
