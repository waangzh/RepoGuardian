"""受控代码上下文检索：所有路径、符号与搜索范围均受服务端索引约束。"""

import re
from pathlib import Path
from typing import Any

from app.models.review import ContextRetrievalPlan, RetrievalRelevanceType, ReviewToolScope
from app.tools.base import BaseTool
from app.tools.git_tool import GitTool


class ContextRetrievalPlanError(ValueError):
    """检索计划引用了索引之外的资源或无法安全执行。"""


class CodeSearchTool(BaseTool):
    name = "code_search"
    description = "Retrieve indexed code context using a bounded structured plan."

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        snippets = await self.retrieve_context(
            changed_files=kwargs["changed_files"],
            symbol_index=kwargs["symbol_index"],
            file_index=kwargs["file_index"],
            repo_path=kwargs["repo_path"],
            plan=kwargs.get("plan"),
            failure_fingerprints=kwargs.get("failure_fingerprints"),
            scope=kwargs.get("scope"),
        )
        return {"context_snippets": snippets}

    async def retrieve_context(
        self,
        changed_files: list[dict[str, Any]],
        symbol_index: list[dict[str, Any]],
        file_index: list[dict[str, Any]],
        repo_path: str,
        plan: ContextRetrievalPlan | dict[str, Any] | None = None,
        failure_fingerprints: list[dict[str, Any]] | None = None,
        scope: ReviewToolScope | dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """执行字面量、索引驱动的检索；绝不把模型文本转换为 Shell 或正则。"""
        normalized_scope = (
            scope if isinstance(scope, ReviewToolScope)
            else ReviewToolScope.model_validate(scope) if scope is not None else None
        )
        if normalized_scope is not None:
            readable = normalized_scope.readable_files
            file_index = [item for item in file_index if item.get("path") in readable]
            symbol_index = [item for item in symbol_index if item.get("file") in readable]
            changed_files = [item for item in changed_files if item.get("file_path") in readable]
        normalized_plan = _normalize_plan(plan, changed_files)
        if normalized_scope is not None:
            unknown = set(normalized_plan.target_files) - normalized_scope.readable_files
            if unknown:
                raise ContextRetrievalPlanError(
                    f"target files are outside review unit scope: {sorted(unknown)}"
                )
            normalized_plan.max_results = min(
                normalized_plan.max_results, normalized_scope.max_search_results
            )
        _validate_plan_against_indexes(normalized_plan, file_index, symbol_index)
        git_tool = GitTool()
        snippets: list[dict[str, Any]] = []
        target_files = set(normalized_plan.target_files)
        target_symbols = set(normalized_plan.target_symbols)
        selected_symbols = [
            symbol for symbol in symbol_index
            if (not target_files or symbol["file"] in target_files)
            and (not target_symbols or symbol["symbol"] in target_symbols)
        ]
        relevance = set(normalized_plan.relevance_types)

        def add_symbol(symbol: dict[str, Any], kind: str) -> None:
            _append_symbol_snippet(snippets, git_tool, repo_path, symbol, kind)

        if RetrievalRelevanceType.direct in relevance:
            for symbol in selected_symbols:
                add_symbol(symbol, "direct")

        if normalized_plan.include_callers or RetrievalRelevanceType.caller in relevance:
            for target in selected_symbols:
                for candidate in symbol_index:
                    if candidate is not target and any(
                        _call_matches_symbol(call, target["symbol"])
                        for call in candidate.get("calls", [])
                    ):
                        add_symbol(candidate, "caller")

        if normalized_plan.include_callees or RetrievalRelevanceType.callee in relevance:
            for target in selected_symbols:
                for call in target.get("calls", []):
                    for candidate in symbol_index:
                        if _call_matches_symbol(call, candidate["symbol"]):
                            add_symbol(candidate, "callee")

        source_files = target_files or {symbol["file"] for symbol in selected_symbols}
        if normalized_plan.include_tests or RetrievalRelevanceType.test in relevance:
            for source_file in sorted(source_files):
                for test_file in _find_test_files(source_file, file_index):
                    _append_file_snippet(snippets, git_tool, repo_path, test_file, "test", None, 2_000)

        if RetrievalRelevanceType.module_config in relevance:
            for source_file in sorted(source_files):
                for config_file in _find_module_config_files(source_file, file_index):
                    _append_file_snippet(
                        snippets, git_tool, repo_path, config_file, "module_config", None, 2_000
                    )
                _append_constant_snippets(snippets, git_tool, repo_path, source_file)

        if RetrievalRelevanceType.adjacent in relevance:
            for target in selected_symbols:
                for adjacent in _adjacent_symbols(target, symbol_index, normalized_plan.depth):
                    add_symbol(adjacent, "adjacent")

        if RetrievalRelevanceType.type_definition in relevance:
            for source_file in sorted(source_files):
                for candidate in symbol_index:
                    if candidate["file"] == source_file and (
                        candidate.get("type") == "class" or "Protocol" in candidate.get("signature", "")
                    ):
                        add_symbol(candidate, "type_definition")

        if RetrievalRelevanceType.import_source in relevance:
            for source_file in sorted(source_files):
                source = next((item for item in file_index if item["path"] == source_file), None)
                for imported in (source or {}).get("imports", []):
                    for candidate in symbol_index:
                        if Path(candidate["file"]).stem == imported:
                            add_symbol(candidate, "import_source")

        if RetrievalRelevanceType.failure_location in relevance:
            known_files = {item["path"] for item in file_index}
            for fingerprint in failure_fingerprints or []:
                file_path = fingerprint.get("file_path")
                line_no = fingerprint.get("line_no")
                if file_path in known_files and isinstance(line_no, int) and line_no > 0:
                    _append_line_window(
                        snippets, git_tool, repo_path, file_path, line_no, "failure_location"
                    )

        if RetrievalRelevanceType.text in relevance:
            _append_literal_search_results(
                snippets,
                git_tool,
                repo_path,
                file_index,
                normalized_plan.search_terms,
                target_files,
            )

        result = _dedupe_and_limit(snippets, normalized_plan.max_results)
        if normalized_scope is not None:
            for snippet in result:
                lines = snippet.get("content", "").splitlines()
                if len(lines) > normalized_scope.max_lines_per_read:
                    snippet["content"] = "\n".join(lines[:normalized_scope.max_lines_per_read]) + "\n...(truncated)"
                snippet["review_unit_id"] = normalized_scope.review_unit_id
        return result


def _normalize_plan(
    plan: ContextRetrievalPlan | dict[str, Any] | None,
    changed_files: list[dict[str, Any]],
) -> ContextRetrievalPlan:
    """兼容旧工具调用；图节点始终显式提供计划。"""
    if plan is not None:
        return plan if isinstance(plan, ContextRetrievalPlan) else ContextRetrievalPlan.model_validate(plan)
    return ContextRetrievalPlan(
        reason="兼容调用：读取变更符号与相关测试",
        target_files=[item["file_path"] for item in changed_files if item.get("file_path")],
        relevance_types=[
            RetrievalRelevanceType.direct,
            RetrievalRelevanceType.caller,
            RetrievalRelevanceType.test,
        ],
        include_callers=True,
        include_tests=True,
    )


def _validate_plan_against_indexes(
    plan: ContextRetrievalPlan,
    file_index: list[dict[str, Any]],
    symbol_index: list[dict[str, Any]],
) -> None:
    indexed_files = {item.get("path") for item in file_index}
    unknown_files = sorted(set(plan.target_files) - indexed_files) if indexed_files else []
    if unknown_files:
        raise ContextRetrievalPlanError(f"target files are not in repository index: {unknown_files}")
    indexed_symbols = {item.get("symbol") for item in symbol_index}
    unknown_symbols = sorted(set(plan.target_symbols) - indexed_symbols)
    if unknown_symbols:
        raise ContextRetrievalPlanError(f"target symbols are not in symbol index: {unknown_symbols}")


def _append_symbol_snippet(
    snippets: list[dict[str, Any]],
    git_tool: GitTool,
    repo_path: str,
    symbol: dict[str, Any],
    relevance: str,
) -> None:
    content = _read_snippet(
        git_tool, repo_path, symbol["file"], symbol["start_line"], symbol["end_line"]
    )
    if content:
        snippets.append({
            "file": symbol["file"],
            "start_line": symbol["start_line"],
            "end_line": symbol["end_line"],
            "content": content,
            "relevance": relevance,
            "symbol": symbol["symbol"],
        })


def _append_file_snippet(
    snippets: list[dict[str, Any]],
    git_tool: GitTool,
    repo_path: str,
    file_path: str,
    relevance: str,
    symbol: str | None,
    limit: int,
) -> None:
    content = git_tool.get_file_content(repo_path, file_path)
    if content.strip():
        snippets.append({
            "file": file_path,
            "start_line": 1,
            "end_line": content.count("\n") + 1,
            "content": _truncate(content, limit),
            "relevance": relevance,
            "symbol": symbol,
        })


def _append_line_window(
    snippets: list[dict[str, Any]],
    git_tool: GitTool,
    repo_path: str,
    file_path: str,
    line_no: int,
    relevance: str,
) -> None:
    start = max(1, line_no - 3)
    end = line_no + 3
    content = _read_snippet(git_tool, repo_path, file_path, start, end)
    if content:
        snippets.append({
            "file": file_path,
            "start_line": start,
            "end_line": end,
            "content": content,
            "relevance": relevance,
            "symbol": None,
        })


def _append_constant_snippets(
    snippets: list[dict[str, Any]], git_tool: GitTool, repo_path: str, file_path: str
) -> None:
    content = git_tool.get_file_content(repo_path, file_path)
    for line_no, line in enumerate(content.splitlines(), start=1):
        if re.match(r"^[A-Z][A-Z0-9_]{1,80}\s*=", line.strip()):
            _append_line_window(snippets, git_tool, repo_path, file_path, line_no, "module_config")


def _append_literal_search_results(
    snippets: list[dict[str, Any]],
    git_tool: GitTool,
    repo_path: str,
    file_index: list[dict[str, Any]],
    search_terms: list[str],
    target_files: set[str],
) -> None:
    if not search_terms:
        return
    candidates = [item["path"] for item in file_index if not target_files or item["path"] in target_files]
    for file_path in sorted(candidates):
        content = git_tool.get_file_content(repo_path, file_path)
        for line_no, line in enumerate(content.splitlines(), start=1):
            if any(term.casefold() in line.casefold() for term in search_terms):
                _append_line_window(snippets, git_tool, repo_path, file_path, line_no, "text")


def _adjacent_symbols(
    target: dict[str, Any], symbol_index: list[dict[str, Any]], depth: int
) -> list[dict[str, Any]]:
    if depth == 0:
        return []
    same_file = sorted(
        (symbol for symbol in symbol_index if symbol["file"] == target["file"]),
        key=lambda symbol: (symbol["start_line"], symbol["end_line"]),
    )
    target_index = next((index for index, symbol in enumerate(same_file) if symbol is target), None)
    if target_index is None:
        return []
    return [
        symbol for index, symbol in enumerate(same_file)
        if index != target_index and abs(index - target_index) <= depth
    ]


def _find_module_config_files(source_path: str, file_index: list[dict[str, Any]]) -> list[str]:
    directory = Path(source_path).parent.as_posix()
    allowed_names = {"pyproject.toml", "setup.cfg", "setup.py", "tox.ini", "package.json"}
    return [
        item["path"] for item in file_index
        if Path(item["path"]).name in allowed_names
        and (directory == "." or Path(item["path"]).parent.as_posix() in {directory, "."})
    ]


def _call_matches_symbol(call: str, symbol: str) -> bool:
    return call == symbol or call.endswith(f".{symbol}") or symbol.endswith(f".{call}")


def _read_snippet(git_tool: GitTool, repo_path: str, file_path: str, start: int, end: int) -> str | None:
    content = git_tool.get_file_content(repo_path, file_path, start, end)
    return _truncate(content, 3_000) if content.strip() else None


def _truncate(content: str, limit: int) -> str:
    return content if len(content) <= limit else content[:limit] + "\n...(truncated)"


def _find_test_files(source_path: str, file_index: list[dict[str, Any]]) -> list[str]:
    base = Path(source_path).stem
    candidates: list[str] = []
    patterns = [
        rf"tests?[/\\](test[/\\])?{re.escape(base)}.*\.py$",
        rf"tests?[/\\]test_{re.escape(base)}\.py$",
        rf"test[/\\]{re.escape(base)}.*\.py$",
    ]
    for file_item in file_index:
        if file_item.get("language") != "python" or file_item.get("path") == source_path:
            continue
        if any(re.search(pattern, file_item["path"]) for pattern in patterns):
            candidates.append(file_item["path"])
    return candidates[:4]


def _dedupe_and_limit(snippets: list[dict[str, Any]], max_results: int) -> list[dict[str, Any]]:
    seen: set[tuple[str, int, int]] = set()
    deduped: list[dict[str, Any]] = []
    for snippet in sorted(snippets, key=_relevance_rank):
        key = (snippet["file"], snippet["start_line"], snippet["end_line"])
        if key not in seen:
            seen.add(key)
            deduped.append(snippet)
    return deduped[:max_results]


def _relevance_rank(item: dict[str, Any]) -> int:
    order = {
        "direct": 0,
        "caller": 1,
        "callee": 2,
        "test": 3,
        "failure_location": 4,
        "type_definition": 5,
        "import_source": 6,
        "module_config": 7,
        "adjacent": 8,
        "text": 9,
    }
    return order.get(item.get("relevance", "text"), 99)
