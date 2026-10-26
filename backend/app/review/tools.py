"""Small native tool adapters; search results are hints, never source evidence."""

import asyncio
import json
from functools import wraps

from langchain.tools import ToolRuntime, tool

from app.review.context import AgentContext
from app.review.tool_scope import is_sensitive_repository_change, is_sensitive_repository_path


def safe_read(function):
    @wraps(function)
    async def guarded(*args, **kwargs):
        try:
            runtime = kwargs.get("runtime")
            guard = runtime.context.check_active if runtime else None
            if guard:
                await guard()
            result = await function(*args, **kwargs)
            if guard:
                await guard()
            if len(json.dumps(result)) > 24000:
                return {"ok": False, "error": "tool_output_too_large", "truncated": True}
            return result
        except (ValueError, OSError) as exc:
            import traceback
            return {"ok": False, "error": "read_rejected",
                    "error_type": type(exc).__name__,
                    "error_stage": traceback.extract_tb(exc.__traceback__)[-1].name,
                    "truncated": False}
    return guarded


@tool
@safe_read
async def read_file(path: str, start: int, end: int, runtime: ToolRuntime[AgentContext]) -> dict:
    """Read a bounded range from the frozen HEAD; returns trusted source evidence."""
    return await runtime.context.evidence.read(path, start, end)


@tool
@safe_read
async def read_base_file(path: str, start: int, end: int, runtime: ToolRuntime[AgentContext]) -> dict:
    """Read frozen BASE source, including files deleted by the PR."""
    return await runtime.context.evidence.read(path, start, end, "base")


@tool
@safe_read
async def read_diff(path: str, runtime: ToolRuntime[AgentContext]) -> dict:
    """Read the fixed PR diff for an authorized path. Diff text is not source evidence."""
    service = runtime.context.evidence
    if path not in service.allowed_paths or is_sensitive_repository_path(path):
        raise ValueError("path_not_authorized")
    matches = [item for item in runtime.context.changed_files if item["file_path"] == path]
    if any(is_sensitive_repository_change(path, item.get("old_file_path")) for item in matches):
        raise ValueError("sensitive_change")
    result = {"ok": True, "files": matches, "source": "fixed_diff", "truncated": False}
    if len(json.dumps(result)) > service.max_output_chars:
        return {"ok": False, "error": "diff_too_large_use_source_ranges", "truncated": True}
    return result


@tool
@safe_read
async def read_symbol(path: str, symbol: str, runtime: ToolRuntime[AgentContext]) -> dict:
    """Read a unique HEAD symbol definition. Use a symbol ID for overloaded names."""
    matches = [item for item in runtime.context.symbols if item.get("file") == path
               and symbol in {item.get("symbol"), item.get("symbol_id")}]
    if len(matches) != 1:
        raise ValueError("symbol_missing_or_ambiguous")
    item = matches[0]
    return await runtime.context.evidence.read(path, item["start_line"], item["end_line"])


@tool
@safe_read
async def search_code(query: str, path: str, runtime: ToolRuntime[AgentContext]) -> dict:
    """Literal search in one authorized frozen HEAD file. Read matching ranges for evidence."""
    if not query or len(query) > 200:
        raise ValueError("invalid_search")
    service = runtime.context.evidence
    _, source = await asyncio.to_thread(service._source, path, "head")
    matches = [{"path": path, "line": i, "text": text[:300]}
               for i, text in enumerate(source.splitlines(), 1) if query in text]
    return {"ok": True, "source": "search_hint", "matches": matches[:12],
            "truncated": len(matches) > 12}


async def _relations(symbol: str, runtime, direction: str | None):
    context = runtime.context
    matches = [item for item in context.symbols
               if symbol in {item.get("symbol_id"), item.get("symbol")}
               and item.get("file") in context.evidence.allowed_paths]
    identities = {item.get("symbol_id") for item in matches}
    edges = [edge for edge in context.repository_graph.get("edges", [])
             if ((edge.get("target") in identities if direction == "in" else
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
    if index < 0 or index >= len(runtime.context.discussions):
        return {"ok": False, "error": "discussion_not_found"}
    item = runtime.context.discussions[index]
    if len(json.dumps(item)) > 12000:
        return {"ok": False, "error": "discussion_too_large", "truncated": True}
    return {"ok": True, "source": "untrusted_discussion", "discussion": item}


REVIEW_TOOLS = [
    read_file, read_base_file, read_diff, read_symbol, search_code,
    find_references, get_callers, get_callees, read_discussion,
]
