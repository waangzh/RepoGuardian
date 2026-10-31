"""The entire agent tool surface; runtime authority is never a model argument."""

import asyncio
import json
import logging
from inspect import signature
from functools import wraps
from typing import Literal

from langchain.tools import ToolRuntime, tool

from app.review.context import AgentContext
from app.review.domain import CandidateInput
from app.review.findings import admit_candidate
from app.review.retrieval import find_paths, search
from app.review.evidence import SourceReadError

logger = logging.getLogger("RepoGuardian.Review.Tools")


def bounded(function):
    parameters = signature(function)
    @wraps(function)
    async def execute(*args, **kwargs):
        context = kwargs["runtime"].context
        await context.check_active()
        context.tool_calls += 1
        if context.tool_calls > context.provider.run.configuration.options.limits.tool_calls:
            logger.warning("工具调用被预算阻止 review_id=%.8s work_id=%.8s tool=%s reason=tool_call_limit",
                           context.provider.run.id, context.work.id, function.__name__)
            return {"ok": False, "error": "tool_call_limit"}
        try:
            result = await function(*args, **kwargs)
        except SourceReadError as exc:
            logger.warning("工具调用被拒绝 review_id=%.8s work_id=%.8s tool=%s reason=%s",
                           context.provider.run.id, context.work.id, function.__name__, exc.code)
            result = {"ok": False, "error": exc.code, **exc.details}
        except (ValueError, OSError) as exc:
            logger.warning("工具调用被拒绝 review_id=%.8s work_id=%.8s tool=%s error_type=%s",
                           context.provider.run.id, context.work.id, function.__name__, type(exc).__name__)
            result = {"ok": False, "error": "source_or_submission_rejected"}
        await context.check_active()
        # Submission receipts remain idempotent; never block saving a candidate.
        if function.__name__ != "submit_candidate":
            bound = parameters.bind(*args, **kwargs)
            bound.apply_defaults()
            arguments = {key: value for key, value in bound.arguments.items() if key != "runtime"}
            repetition = context.note_tool_result(function.__name__, arguments, result)
            if repetition == 3:
                return {**result, "progress_warning": "Repeated request returned no new information. "
                        "Use the evidence already received, change the query, or finish."}
        logger.debug("工具调用完成 review_id=%.8s work_id=%.8s tool=%s",
                     context.provider.run.id, context.work.id, function.__name__)
        return result
    return execute


@tool
@bounded
async def read_file(path: str, revision: Literal["base", "head"], start: int, end: int,
                    runtime: ToolRuntime[AgentContext]) -> dict:
    """Read complete fixed source lines, 1-based inclusive, at most 240 lines."""
    return await runtime.context.read(path, start, end, revision)


@tool
@bounded
async def read_diff(hunk_id: str, runtime: ToolRuntime[AgentContext]) -> dict:
    """Deliver a missing required hunk member, using an ID from the frozen unit."""
    unit = next(unit for unit in runtime.context.provider.run.plan.units
                if unit.id == runtime.context.work.unit_id)
    if hunk_id not in unit.required_hunk_ids:
        raise ValueError("hunk_not_in_unit")
    return await runtime.context.provider.material(runtime.context, hunk_id)


@tool
@bounded
async def search_code(query: str, runtime: ToolRuntime[AgentContext], path: str = "",
                      glob: str = "", revision: Literal["base", "head"] = "head",
                      cursor: str = "", limit: int = 12) -> dict:
    """Bounded literal search. Results are hints, read source before citing them.

    Continue next_cursor with the same filters. An empty partial page is not absence.
    """
    return await search(runtime.context, query, path, glob, revision, cursor, limit)


@tool
@bounded
async def find_files(runtime: ToolRuntime[AgentContext], query: str = "", path: str = "",
                     glob: str = "", cursor: str = "", limit: int = 80) -> dict:
    """Page through the authorized tracked file inventory."""
    return await asyncio.to_thread(find_paths, runtime.context, query, path, glob, cursor, limit)


@tool
@bounded
async def submit_candidate(proposal: CandidateInput, runtime: ToolRuntime[AgentContext]) -> dict:
    """Persist a concrete defect claim for independent verification immediately.

    Success means saved, never confirmed. Cite only source_ref handles actually received.
    """
    context = runtime.context
    candidate = admit_candidate(proposal, context, runtime.tool_call_id)
    candidate_id = await asyncio.to_thread(context.provider.store.submit,
        context.provider.run.id, context.work, candidate, context.provider.lease)
    progress = "candidate:" + candidate_id
    if progress not in context.progress_keys:
        context.progress_keys.add(progress)
        context.round_progress = True
        context.round_tools += 1
    logger.info("候选已保存并排队核验 review_id=%.8s work_id=%.8s candidate_id=%.8s",
                context.provider.run.id, context.work.id, candidate_id)
    return {"ok": True, "candidate_id": candidate_id, "status": "unverified"}


@tool
@bounded
async def read_discussion(index: int, runtime: ToolRuntime[AgentContext]) -> dict:
    """Read frozen untrusted discussion background; it is never source evidence."""
    context = runtime.context
    if not context.provider.run.configuration.options.include_pr_discussions:
        raise ValueError("discussion_disabled")
    if index < 0 or index >= len(context.provider.discussions):
        raise ValueError("discussion_not_found")
    text = json.dumps(context.provider.discussions[index], ensure_ascii=False)
    if len(text) > 12000:
        raise ValueError("discussion_too_large")
    return {"source": "untrusted_discussion", "content": text}


@tool
@bounded
async def read_symbol(path: str, symbol: str, runtime: ToolRuntime[AgentContext],
                      revision: Literal["base", "head"] = "head") -> dict:
    """Parse one fixed file on demand and read a unique symbol's complete source."""
    context = runtime.context
    found = await context.provider.index.symbol(path, symbol, revision)
    return await context.read(path, found["start_line"], found["end_line"], revision)


@tool
@bounded
async def find_relations(path: str, symbol: str, runtime: ToolRuntime[AgentContext],
                         direction: Literal["in", "out", "both"] = "both", scope: str = "") -> dict:
    """Find bounded caller/callee/reference hints, parsing likely related files only."""
    return await runtime.context.provider.index.relations(path, symbol, direction, scope)


READ_TOOLS = [read_file, read_diff, search_code, find_files, read_symbol, find_relations]
