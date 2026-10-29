import asyncio

from langgraph.runtime import Runtime

from app.review.context import ReviewContext
from app.review.state import ReviewState
from app.review.planning import plan_review


async def prepare(state: ReviewState, runtime: Runtime[ReviewContext]):
    sources = await runtime.context.sources()
    if state["review_id"] != sources.evidence.snapshot.review_id:
        raise ValueError("review_snapshot_identity_mismatch")
    await runtime.context.emit(phase="prepare", status="completed")
    return {"snapshot": sources.evidence.snapshot.model_dump(), "status": "planning"}



async def plan(state: ReviewState, runtime: Runtime[ReviewContext]):
    context = runtime.context
    sources = await context.sources()
    confirmed = await asyncio.to_thread(plan_review,
        sources.changed_files, depth=context.depth, selected_paths=context.selected_paths,
        focus=context.focus, limits=context.limits)
    # Index hints may regroup admitted files, but must not rerank excluded files into the budget.
    admitted = frozenset(path for unit in confirmed.units for path in unit.changed_paths)
    review_plan = await asyncio.to_thread(plan_review,
        sources.changed_files, depth=context.depth, selected_paths=admitted,
        focus=context.focus, repository_graph=sources.repository_graph, symbols=sources.symbols,
        limits=context.limits)
    if frozenset(path for unit in review_plan.units for path in unit.changed_paths) != admitted:
        review_plan = confirmed
    limitations = list(dict.fromkeys(confirmed.limitations + review_plan.limitations))
    await context.emit(phase="planning", units=[item.model_dump() for item in review_plan.units],
                       assignments=[item.model_dump() for item in review_plan.assignments],
                       limitations=limitations)
    return {"goals": [item.model_dump() for item in confirmed.goals],
            "review_units": [item.model_dump() for item in review_plan.units],
            "assignments": [item.model_dump() for item in review_plan.assignments],
            "coverage_limitations": limitations, "review_depth": context.depth,
            "status": "discovering"}
