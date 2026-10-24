"""Bounded source preparation for the frozen review-stability-v1 contract."""

from app.models.review_runtime import RuntimeReadArguments
from app.review.tool_scope import is_sensitive_repository_path

CONTRACT = "review-stability-v1"
MAX_PREPARATORY_READS = 12
MAX_RECOVERY_ROUNDS = 2


def source_requests(symbols, readable_files, max_lines, *, limit=MAX_PREPARATORY_READS):
    """Keep whole definitions or report a capacity gap; never truncate a body."""
    ranges = set()
    for symbol in symbols:
        path = symbol.get("file")
        start, end = symbol.get("start_line"), symbol.get("end_line")
        if (path not in readable_files or is_sensitive_repository_path(path)
                or type(start) is not int or type(end) is not int or not 1 <= start <= end <= 1_000_000):
            continue
        ranges.add((path, start, end))
    requests = []
    for path, start, end in sorted(ranges):
        if len(requests) + (end - start) // max_lines + 1 > limit:
            return requests, True
        chunks = [RuntimeReadArguments(file_path=path, start_line=line,
                    end_line=min(end, line + max_lines - 1))
                  for line in range(start, end + 1, max_lines)]
        requests.extend(chunks)
    return requests, False


def inspection_requests(executor, symbol_index, graph, max_lines):
    goals = executor.current_goals()
    definitions = [symbol for symbol in symbol_index
                   if any(not goal.base_side and symbol.get("file") == goal.target_ref
                          and type(symbol.get("start_line")) is int and type(symbol.get("end_line")) is int
                          and symbol["start_line"] <= goal.end_line
                          and symbol["end_line"] >= goal.start_line for goal in goals)]
    # Prefer the innermost changed definition over its enclosing class/module.
    definitions = [symbol for symbol in definitions if not any(
        other is not symbol and other.get("file") == symbol.get("file")
        and symbol["start_line"] <= other["start_line"] <= other["end_line"] <= symbol["end_line"]
        and (symbol["start_line"], symbol["end_line"]) != (other["start_line"], other["end_line"])
        for other in definitions)]
    own, _ = source_requests(definitions, executor.readable_files, max_lines)
    ids = {symbol.get("symbol_id") or symbol.get("id")
           or f"{symbol.get('file')}::{symbol.get('symbol')}" for symbol in definitions}
    targets = {edge.get("target") for edge in graph.get("edges", [])
               if resolved_call(edge) and edge.get("source") in ids}
    related = [symbol for symbol in graph.get("symbols", []) if symbol.get("id") in targets]
    extra, _ = source_requests(related, executor.readable_files, max_lines,
                               limit=max(0, MAX_PREPARATORY_READS - len(own)))
    return [request.model_dump(mode="json") for request in [*own, *extra]]


def resolved_call(edge):
    return (edge.get("type") == "calls" and edge.get("resolution_status") == "resolved"
            and edge.get("resolution_basis") in {"python_lexical_import", "qualified_import_module"}
            and edge.get("relationship_level") != "search_hint")


def companion_definitions(edge, graph, symbols):
    """One-hop caller context includes outer(inner(...)) without guessing bindings."""
    selected = {}
    for other in graph.get("edges", []):
        if not resolved_call(other) or other.get("source") != edge.get("source"):
            continue
        symbol = symbols.get(str(other.get("target")))
        if symbol is not None:
            selected[str(symbol.get("id"))] = symbol
    return [selected[key] for key in sorted(selected)]


def request_key(request):
    return (request.file_path, request.start_line, request.end_line)


def covered(executor, request):
    goal = next(iter(executor.goals.values())).model_copy(update={
        "target_ref": request.file_path, "start_line": request.start_line,
        "end_line": request.end_line, "target_kind": "source_range"})
    active = sorted(executor.active_evidence_ids)
    return executor.evidence_error(active) is None and executor._covers(goal, active)
