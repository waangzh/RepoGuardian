from typing import Any

from app.agents.providers import LLMProvider, MockProvider
from app.models.review import ChangedFile, PullRequestInfo, ReviewIssue


class ReviewAgent:
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
        if not isinstance(self._provider, MockProvider):
            return await self._provider.review(pr_info, changed_files, diff_text, model)
        return await self._provider.review(pr_info, changed_files, diff_text, model)


def _build_context_text(snippets: list[dict[str, Any]]) -> str:
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
