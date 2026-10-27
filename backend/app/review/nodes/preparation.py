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
    goals, assignments, _ = plan_review(
        sources.changed_files, depth=context.depth, selected_paths=context.selected_paths, focus=context.focus,
        repository_graph=sources.repository_graph, symbols=sources.symbols)
    await context.emit(phase="planning", assignments=[item.model_dump() for item in assignments])
    return {"goals": [item.model_dump() for item in goals],
            "assignments": [item.model_dump() for item in assignments], "status": "discovering"}
