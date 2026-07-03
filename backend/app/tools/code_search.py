"""代码搜索工具 —— 根据变更文件检索相关代码上下文。

检索策略：
    1. 直接片段：变更符号的源代码
    2. 调用者：调用变更符号的其他函数/方法
    3. 测试文件：与变更源文件匹配的测试文件

结果按相关性排序：direct > caller > test > adjacent
"""

import re
from pathlib import Path
from typing import Any

from app.tools.base import BaseTool
from app.tools.git_tool import GitTool


class CodeSearchTool(BaseTool):
    name = "code_search"
    description = "Retrieve related code context for changed files."

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        snippets = await self.retrieve_context(
            changed_files=kwargs["changed_files"],
            symbol_index=kwargs["symbol_index"],
            file_index=kwargs["file_index"],
            repo_path=kwargs["repo_path"],
        )
        return {"context_snippets": snippets}

    async def retrieve_context(
        self,
        changed_files: list[dict[str, Any]],
        symbol_index: list[dict[str, Any]],
        file_index: list[dict[str, Any]],
        repo_path: str,
    ) -> list[dict[str, Any]]:
        """检索变更文件相关的代码片段：直接符号 + 调用者 + 测试文件，去重排序。"""
        git_tool = GitTool()
        snippets: list[dict[str, Any]] = []

        changed_paths = {f["file_path"] for f in changed_files}
        changed_symbols = [s for s in symbol_index if s["file"] in changed_paths]

        # 1) 直接片段：变更文件中的函数/类/方法源码
        for sym in changed_symbols:
            snippet = _read_snippet(git_tool, repo_path, sym["file"], sym["start_line"], sym["end_line"])
            if snippet:
                snippets.append({
                    "file": sym["file"],
                    "start_line": sym["start_line"],
                    "end_line": sym["end_line"],
                    "content": snippet,
                    "relevance": "direct",
                    "symbol": sym["symbol"],
                })

            # 2) 调用者：其他文件中调用变更符号的函数
            for s in symbol_index:
                if s is sym:
                    continue
                if sym["symbol"] in s.get("calls", []):
                    snip = _read_snippet(git_tool, repo_path, s["file"], s["start_line"], s["end_line"])
                    if snip:
                        snippets.append({
                            "file": s["file"],
                            "start_line": s["start_line"],
                            "end_line": s["end_line"],
                            "content": snip,
                            "relevance": "caller",
                            "symbol": s["symbol"],
                        })

        # 3) 测试文件：匹配变更文件的测试文件
        for cf in changed_files:
            test_candidates = _find_test_files(cf["file_path"], file_index)
            for tc in test_candidates[:2]:  # 每个文件最多取 2 个测试文件
                content = git_tool.get_file_content(repo_path, tc)
                if content:
                    snippets.append({
                        "file": tc,
                        "start_line": 1,
                        "end_line": content.count("\n") + 1,
                        "content": _truncate(content, 2000),
                        "relevance": "test",
                        "symbol": None,
                    })

        # 去重：按 (文件, 起始行, 符号名) 作为唯一键
        seen = set()
        deduped: list[dict[str, Any]] = []
        for s in snippets:
            key = (s["file"], s.get("start_line"), s.get("symbol"))
            if key not in seen:
                seen.add(key)
                deduped.append(s)

        # 按相关性排序
        return sorted(deduped, key=_relevance_rank)


def _read_snippet(git_tool: GitTool, repo_path: str, file_path: str, start: int, end: int) -> str | None:
    """读取文件片段，跳过空白内容，截断到 3000 字符。"""
    content = git_tool.get_file_content(repo_path, file_path, start, end)
    if not content.strip():
        return None
    return _truncate(content, 3000)


def _truncate(content: str, limit: int) -> str:
    """超长文本截断并添加 ...(truncated) 标记。"""
    if len(content) <= limit:
        return content
    return content[:limit] + "\n...(truncated)"


def _find_test_files(source_path: str, file_index: list[dict[str, Any]]) -> list[str]:
    """根据源文件路径查找对应的测试文件。"""
    base = Path(source_path).stem
    candidates: list[str] = []
    patterns = [
        rf"tests?[/\\](test[/\\])?{re.escape(base)}.*\.py$",
        rf"tests?[/\\]test_{re.escape(base)}\.py$",
        rf"test[/\\]{re.escape(base)}.*\.py$",
    ]
    for f in file_index:
        if f["language"] != "python":
            continue
        path = f["path"]
        if path == source_path:
            continue
        for pat in patterns:
            if re.search(pat, path):
                candidates.append(path)
                break
    return candidates


def _relevance_rank(item: dict[str, Any]) -> int:
    """相关性排序权重：direct=0, caller=1, test=2, adjacent=3。"""
    order = {"direct": 0, "caller": 1, "test": 2, "adjacent": 3}
    return order.get(item.get("relevance", "adjacent"), 99)
