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
