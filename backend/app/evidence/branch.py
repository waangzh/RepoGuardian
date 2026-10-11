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
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = ast.dump(node.test)
        if node.body and node.lineno <= start <= end <= node.body[-1].end_lineno:
            scopes.append((test, "if"))
        if node.orelse and not isinstance(node.orelse[0], ast.If):
            # AST 不保存 else 关键字位置；仅容许紧邻正文的实际 else 行。
            lower = node.orelse[0].lineno
            lines = source.splitlines()
            if lower > 1 and lines[lower - 2].strip() == "else:":
                lower -= 1
            if lower <= start <= end <= node.orelse[-1].end_lineno:
                scopes.append((test, "else"))
        if claim and claim.arm == "both" and start <= node.lineno and end >= node.end_lineno:
            scopes.append((test, "both"))
    if claim:
        return None if (condition, claim.arm) in scopes else "branch_claim_mismatch"
    return "branch_claim_required" if scopes else None


def counterevidence_branch_error(source: str, anchor) -> str | None:
    """反证必须能在同一段已显示正文中唯一定位，不能跨片段拼接上下文。"""
    matches, offset = [], 0
    while (index := source.find(anchor.existing_code, offset)) >= 0:
        start = source[:index].count("\n") + 1
        end = start + anchor.existing_code.count("\n")
        lines = source.splitlines()
        before = lines[max(0, start - 1 - len(anchor.context_before)):start - 1]
        after = lines[end:end + len(anchor.context_after)]
        if ((not anchor.context_before or before == anchor.context_before)
                and (not anchor.context_after or after == anchor.context_after)):
            matches.append((start, end))
        offset = index + len(anchor.existing_code)
    if len(matches) != 1:
        return "branch_claim_ambiguous"
    return branch_error(source, *matches[0], anchor.branch_claim, fragment=True)
