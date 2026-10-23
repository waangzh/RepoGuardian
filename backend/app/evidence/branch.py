"""只解析 Python 语法与分支范围；不执行目标代码，不证明缺陷语义。"""

import ast
import textwrap


def branch_error(source: str | None, start: int, end: int, claim, *, fragment=False) -> str | None:
    if source is None or len(source) > 2_000_000:
        return "branch_claim_unverified" if claim else None
    try:
        tree = ast.parse(textwrap.dedent(source) if fragment else source)
        condition = ast.dump(ast.parse(claim.condition_code, mode="eval").body) if claim else None
    except (SyntaxError, ValueError, RecursionError):
        return "branch_claim_unverified" if claim else None
    scopes = []
    lines = source.splitlines()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = ast.dump(node.test)
        if node.body and node.lineno <= start <= end <= node.body[-1].end_lineno:
            scopes.append((test, "if"))
        if node.orelse and not (
            isinstance(node.orelse[0], ast.If)
            and lines[node.orelse[0].lineno - 1].lstrip().startswith("elif")
        ):
            # AST 不保存 else 关键字位置；仅容许紧邻正文的实际 else 行。
            lower = node.orelse[0].lineno
            if lower > 1 and lines[lower - 2].strip() == "else:":
                lower -= 1
            if lower <= start <= end <= node.orelse[-1].end_lineno:
                scopes.append((test, "else"))
        if claim and claim.arm == "both" and start <= node.lineno and end >= node.end_lineno:
            scopes.append((test, "both"))
    if claim:
        return None if (condition, claim.arm) in scopes else "branch_claim_mismatch"
    return "branch_claim_required" if scopes else None
