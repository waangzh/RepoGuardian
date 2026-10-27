"""Small native tool adapters; search results are hints, never source evidence."""

import asyncio
import json
from functools import wraps

from langchain.tools import ToolRuntime, tool

from app.review.context import AgentContext
from app.review.evidence import SourceReadError
from app.review.retrieval import diff_page, find_paths, search
from app.review.tool_scope import is_sensitive_repository_path


def safe_read(function):
    @wraps(function)
    async def guarded(*args, **kwargs):
        runtime = kwargs.get("runtime")
        guard = runtime.context.check_active if runtime else None
        if guard:
            await guard()
        try:
            result = await function(*args, **kwargs)
            if len(json.dumps(result)) > 24000:
                return {"ok": False, "error": "tool_output_too_large", "truncated": True}
        except SourceReadError as exc:
            return {"ok": False, "error": exc.code, **exc.details, "truncated": False}
        except (ValueError, OSError) as exc:
            return {"ok": False, "error": "read_rejected",
                    "error_type": type(exc).__name__, "truncated": False}
        if guard:
            await guard()
        return result
    return guarded


@tool
@safe_read
async def read_file(path: str, start: int, end: int, runtime: ToolRuntime[AgentContext]) -> dict:
    """Read a bounded range from the frozen HEAD; returns trusted source evidence."""
    if path in runtime.context.denied_paths:
        raise ValueError("path_not_authorized")
    return await runtime.context.evidence.read(path, start, end)


@tool
@safe_read
async def read_base_file(path: str, start: int, end: int, runtime: ToolRuntime[AgentContext]) -> dict:
    """Read frozen BASE source, including files deleted by the PR."""
    if path in runtime.context.denied_paths:
        raise ValueError("path_not_authorized")
    return await runtime.context.evidence.read(path, start, end, "base")


@tool
@safe_read
async def read_diff(path: str, runtime: ToolRuntime[AgentContext], hunk_index: int | None = None,
                    start: int = 0, cursor: str = "", limit: int = 60) -> dict:
    """List fixed diff hunks, or read hunk_index from zero-based start. Continue as needed.

    Large lines carry line_index/content_offset/continued fragments. Diff is not source evidence.
    """
    return await asyncio.to_thread(diff_page, runtime.context, path, hunk_index, start, cursor, limit)


@tool
@safe_read
async def read_symbol(path: str, symbol: str, runtime: ToolRuntime[AgentContext]) -> dict:
    """Read a unique HEAD symbol definition. Use a symbol ID for overloaded names."""
    if path in runtime.context.denied_paths:
        raise ValueError("path_not_authorized")
    matches = [item for item in runtime.context.symbols if item.get("file") == path
               and symbol in {item.get("symbol"), item.get("symbol_id")}]
    if len(matches) != 1:
        raise ValueError("symbol_missing_or_ambiguous")
    item = matches[0]
    return await runtime.context.evidence.read(path, item["start_line"], item["end_line"])


@tool
@safe_read
async def search_code(query: str, runtime: ToolRuntime[AgentContext], path: str = "",
                      glob: str = "", side: str = "head", cursor: str = "", limit: int = 12) -> dict:
    """Search UTF-8 text at frozen head/base; binary blobs are excluded.

    path selects a file/directory, glob filters paths. Continue next_cursor with
    unchanged filters when more context is needed.
    has_more means the scope is not exhausted, not a failure. An empty partial page
    cannot prove absence. Results are hints; read source for evidence.
    """
    return await search(runtime.context, query, path, glob, side, cursor, limit)


@tool
@safe_read
async def find_files(runtime: ToolRuntime[AgentContext], query: str = "", path: str = "",
                     glob: str = "", cursor: str = "", limit: int = 80) -> dict:
    """List authorized paths by substring, directory and glob. Continue next_cursor as needed."""
    return await asyncio.to_thread(find_paths, runtime.context, query, path, glob, cursor, limit)


async def _relations(symbol: str, runtime, direction: str | None):
    context = runtime.context
    matches = [item for item in context.symbols
               if symbol in {item.get("symbol_id"), item.get("symbol")}
               and item.get("file") in context.evidence.allowed_paths
               and item.get("file") not in context.denied_paths]
    identities = {item.get("symbol_id") or f"{item['file']}::{item['symbol']}" for item in matches}
    edges = [edge for edge in context.repository_graph.get("edges", [])
             if edge.get("source_file") not in context.denied_paths
             and edge.get("target_file") not in context.denied_paths
             and not any(is_sensitive_repository_path(str(edge.get(key)))
                         for key in ("source_file", "target_file") if edge.get(key))
             and ((edge.get("target") in identities if direction == "in" else
                  edge.get("source") in identities) if direction else
                 (edge.get("source") in identities or edge.get("target") in identities))]
    visible = [{key: item.get(key) for key in ("symbol_id", "file", "symbol", "start_line", "end_line")}
               for item in matches[:12]]
    return {"ok": True, "source": "static_relation_hint", "symbols": visible,
            "edges": edges[:24], "truncated": len(matches) > 12 or len(edges) > 24,
            "limitation": "Static relations do not establish runtime binding; read source to verify."}


@tool
@safe_read
async def find_references(symbol: str, runtime: ToolRuntime[AgentContext]) -> dict:
    """Find indexed symbol relations as static hints, without granting source evidence."""
    return await _relations(symbol, runtime, None)


@tool
@safe_read
async def get_callers(symbol: str, runtime: ToolRuntime[AgentContext]) -> dict:
    """Find possible static callers; verify actual binding with source reads."""
    return await _relations(symbol, runtime, "in")


@tool
@safe_read
async def get_callees(symbol: str, runtime: ToolRuntime[AgentContext]) -> dict:
    """Find possible static callees; verify actual binding with source reads."""
    return await _relations(symbol, runtime, "out")


@tool
@safe_read
async def read_discussion(index: int, runtime: ToolRuntime[AgentContext]) -> dict:
    """Read one frozen untrusted PR discussion; it cannot serve as source evidence."""
    if not runtime.context.discussions_enabled:
        return {"ok": False, "error": "discussion_disabled"}
    if index < 0 or index >= len(runtime.context.discussions):
        return {"ok": False, "error": "discussion_not_found"}
    item = runtime.context.discussions[index]
    if len(json.dumps(item)) > 12000:
        return {"ok": False, "error": "discussion_too_large", "truncated": True}
    return {"ok": True, "source": "untrusted_discussion", "discussion": item}


REVIEW_TOOLS = [
    read_file, read_base_file, read_diff, read_symbol, search_code, find_files,
    find_references, get_callers, get_callees, read_discussion,
]
