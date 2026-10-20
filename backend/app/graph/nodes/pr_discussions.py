"""Typed graph boundaries keep supplementary discussion work out of Preview."""

from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from app.graph.state import ReviewRunContext, ReviewState
from app.services.pr_discussions import prepare_discussions_node
from app.services.pr_discussion_recheck import recheck_discussions_node


async def discussion_prepare_node(state: ReviewState, runtime: Runtime[ReviewRunContext], config: RunnableConfig):
    return await prepare_discussions_node(state, runtime, config)


async def discussion_recheck_node(state: ReviewState, runtime: Runtime[ReviewRunContext], config: RunnableConfig):
    return await recheck_discussions_node(state, runtime, config)
