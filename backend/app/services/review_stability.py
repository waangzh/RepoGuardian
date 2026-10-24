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
    modern = executor.spec.stability_contract == "review-stability-v2"
    targets = {edge.get("target") for edge in graph.get("edges", [])
               if edge.get("source") in ids and (resolved_call(edge)
                   or (modern and edge.get("type") == "calls"))}
    related = [symbol for symbol in graph.get("symbols", []) if symbol.get("id") in targets]
    extra, _ = source_requests(related, executor.readable_files, max_lines,
                               limit=max(0, MAX_PREPARATORY_READS - len(own)))
    result = [*own, *extra]
    if modern:
        # Header/import context is visible source only; unique-name edges stay hints.
        headers = [{"file": goal.target_ref, "start_line": 1, "end_line": min(40, max_lines)}
                   for goal in goals if not goal.base_side]
        additions, _ = source_requests(headers, executor.readable_files, max_lines,
            limit=max(0, MAX_PREPARATORY_READS - len(result)))
        result.extend(additions)
    return [request.model_dump(mode="json") for request in result]


def one_hop_consumers(source):
    """Syntax hints for straight-line x = producer(); consumer(x), never bindings."""
    import ast
    import textwrap
    if len(source) > 128_000:
        return set()
    try:
        tree = ast.parse(textwrap.dedent(source))
    except (SyntaxError, ValueError, RecursionError):
        return set()
    functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if len(functions) != 1:
        return set()
    if any(isinstance(node, (ast.Global, ast.Nonlocal)) for node in ast.walk(functions[0])):
        return set()
    body = functions[0].body
    # Do not follow branches, loops, nested scopes, repeated writes or shadowed aliases.
    stores = {}
    for node in ast.walk(functions[0]):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            stores[node.id] = stores.get(node.id, 0) + 1
    result = set()
    for index, statement in enumerate(body):
        if not (isinstance(statement, ast.Assign) and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name) and isinstance(statement.value, ast.Call)):
            continue
        name = statement.targets[0].id
        if stores.get(name) != 1:
            continue
        for later in body[index + 1:]:
            if not isinstance(later, (ast.Assign, ast.Expr, ast.Return)):
                break
            for call in (node for node in ast.walk(later) if isinstance(node, ast.Call)):
                if isinstance(call.func, ast.Name) and any(isinstance(node, ast.Name) and node.id == name
                        for argument in [*call.args, *(item.value for item in call.keywords)]
                        for node in ast.walk(argument)):
                    result.add(call.func.id)
    return result


def auxiliary_requests(executor, graph, path, source, max_lines):
    if not path.endswith(".py"):
        return []
    symbols = {symbol.get("id"): symbol for symbol in graph.get("symbols", [])}
    selected = []
    lines = source.splitlines()
    for identity, caller in sorted(symbols.items()):
        if caller.get("file") != path:
            continue
        own, overflow = source_requests([caller], executor.readable_files, max_lines)
        if not own or overflow or not all(covered(executor, request) for request in own):
            continue
        names = one_hop_consumers("\n".join(lines[caller["start_line"] - 1:caller["end_line"]]))
        for edge in graph.get("edges", []):
            if (edge.get("source") == identity and edge.get("type") == "calls"
                    and edge.get("call_expression") in names and edge.get("target") in symbols):
                selected.append(symbols[edge["target"]])
    requests, _ = source_requests(selected, executor.readable_files, max_lines)
    return [request.model_dump(mode="json") for request in requests]


def resolved_call(edge):
    return (edge.get("type") == "calls" and edge.get("resolution_status") == "resolved"
            and edge.get("resolution_basis") in {"python_lexical_import", "qualified_import_module"}
            and edge.get("relationship_level") != "search_hint")


def companion_definitions(edge, graph, symbols, *, nested_only=False):
    """One-hop caller context includes outer(inner(...)) without guessing bindings."""
    selected = {}
    calls = [other for other in graph.get("edges", [])
             if resolved_call(other) and other.get("source") == edge.get("source")]
    if nested_only:
        calls = _nested_calls(edge, calls, symbols)
    for other in calls:
        symbol = symbols.get(str(other.get("target")))
        if symbol is not None:
            selected[str(symbol.get("id"))] = symbol
    return [selected[key] for key in sorted(selected)]


def _nested_calls(edge, calls, symbols):
    """Follow expression containment per call site, not all sites of a matched callee."""
    caller = symbols.get(str(edge.get("source")), {})
    sites = []
    for other in calls:
        if not other.get("call_sites"):
            return calls
        for site in other["call_sites"]:
            span = site.get("call_range")
            if (not isinstance(span, (list, tuple)) or len(span) != 4
                    or any(type(value) is not int for value in span)
                    or not caller.get("start_line", 1) <= span[0] <= span[2] <= caller.get("end_line", 0)
                    or min(span[1], span[3]) < 0 or tuple(span[:2]) >= tuple(span[2:])
                    or site.get("line") != span[0]):
                # Missing syntax metadata cannot prove that calls are independent.
                return calls
            sites.append((other, tuple(span)))
    selected = {span for other, span in sites if other == edge}
    while True:
        expanded = selected | {span for _, span in sites if any(
            (span[:2] <= known[:2] and known[2:] <= span[2:])
            or (known[:2] <= span[:2] and span[2:] <= known[2:]) for known in selected)}
        if expanded == selected:
            break
        selected = expanded
    return [other for other, span in sites if span in selected]


def request_key(request):
    return (request.file_path, request.start_line, request.end_line)


def covered(executor, request):
    goal = next(iter(executor.goals.values())).model_copy(update={
        "target_ref": request.file_path, "start_line": request.start_line,
        "end_line": request.end_line, "target_kind": "source_range"})
    active = sorted(executor.active_evidence_ids)
    return executor.evidence_error(active) is None and executor._covers(goal, active)
