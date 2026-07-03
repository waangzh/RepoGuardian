"""审查 Agent —— 对 LLMProvider.review() 的薄封装。

主要职责：接收 PR 信息、变更文件和增强 diff，委托给 Provider 执行审查。
"""

from typing import Any

from app.agents.providers import LLMProvider, MockProvider
from app.models.review import ChangedFile, PullRequestInfo, ReviewIssue


class ReviewAgent:
    """代码审查 Agent，封装 LLMProvider 的 review 调用。"""

    def __init__(self, provider: LLMProvider | None = None) -> None:
        self._provider = provider or MockProvider()

    async def review(
        self,
        pr_info: PullRequestInfo,
        changed_files: list[ChangedFile],
        diff_text: str,
        model: str | None,
        context_snippets: list[dict[str, Any]] | None = None,
    ) -> list[ReviewIssue]:
        """调用 LLM 执行代码审查，返回结构化问题列表。"""
        if not isinstance(self._provider, MockProvider):
            return await self._provider.review(pr_info, changed_files, diff_text, model)
        return await self._provider.review(pr_info, changed_files, diff_text, model)


def _build_context_text(snippets: list[dict[str, Any]]) -> str:
    """将上下文片段列表拼接为 Markdown 格式文本（当前未直接使用，由 review_node 拼接）。"""
    if not snippets:
        return "无相关上下文。"
    lines: list[str] = []
    for s in snippets:
        lines.append(
            f"### {s.get('relevance', 'unknown')} | {s['file']}:{s.get('start_line', '?')}"
        )
        lines.append("```python")
        lines.append(s.get("content", ""))
        lines.append("```")
        lines.append("")
    return "\n".join(lines)
