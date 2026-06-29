import json
import re
from abc import ABC, abstractmethod
from typing import Any

import httpx
from pydantic import TypeAdapter, ValidationError

from app.models.review import ChangedFile, PullRequestInfo, ReviewIssue


class LLMProviderError(RuntimeError):
    pass


class LLMProvider(ABC):
    @abstractmethod
    async def review(
        self,
        pr: PullRequestInfo,
        changed_files: list[ChangedFile],
        diff_text: str,
        model: str | None,
    ) -> list[ReviewIssue]:
        raise NotImplementedError


class MockProvider(LLMProvider):
    async def review(
        self,
        pr: PullRequestInfo,
        changed_files: list[ChangedFile],
        diff_text: str,
        model: str | None,
    ) -> list[ReviewIssue]:
        for file in changed_files:
            for hunk in file.hunks:
                if hunk.added_lines:
                    line = hunk.added_lines[0]
                    return [
                        ReviewIssue(
                            file_path=file.file_path,
                            line_no=line.line_no,
                            severity="low",
                            category="maintainability",
                            title="Mock 审查提示",
                            description="当前使用 MockProvider，仅用于验证任务闭环，不代表真实代码问题。",
                            suggestion="配置 OPENAI_API_KEY 并设置 REPOGUARDIAN_PROVIDER=openai 以启用真实审查。",
                            confidence=0.2,
                        )
                    ]
        return []


class OpenAICompatibleProvider(LLMProvider):
    def __init__(self, api_key: str | None, base_url: str, default_model: str) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model
        self._issue_adapter = TypeAdapter(list[ReviewIssue])

    async def review(
        self,
        pr: PullRequestInfo,
        changed_files: list[ChangedFile],
        diff_text: str,
        model: str | None,
    ) -> list[ReviewIssue]:
        if not self._api_key:
            raise LLMProviderError("OPENAI_API_KEY 未配置，无法使用 openai provider")

        prompt = self._build_prompt(pr, changed_files, diff_text)
        payload = {
            "model": model or self._default_model,
            "temperature": 0.1,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是严格的代码审查 Agent。只报告有明确证据的问题，"
                        "输出必须是 JSON 数组，不要 Markdown。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        }
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                f"{self._base_url}/chat/completions", headers=headers, json=payload
            )

        if response.status_code >= 400:
            raise LLMProviderError(f"LLM 请求失败：{response.status_code} {response.text[:500]}")

        content = response.json()["choices"][0]["message"]["content"]
        return self._parse_issues(content)

    def _parse_issues(self, content: str) -> list[ReviewIssue]:
        try:
            raw = json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"\[[\s\S]*\]", content)
            if not match:
                raise LLMProviderError("LLM 输出不是合法 JSON 数组")
            raw = json.loads(match.group(0))

        try:
            return self._issue_adapter.validate_python(raw)
        except ValidationError as exc:
            raise LLMProviderError(f"LLM issue 结构校验失败：{exc}") from exc

    @staticmethod
    def _build_prompt(pr: PullRequestInfo, changed_files: list[ChangedFile], diff_text: str) -> str:
        files_payload: list[dict[str, Any]] = [
            file.model_dump(exclude={"hunks": {"__all__": {"removed_lines"}}})
            for file in changed_files
        ]
        limited_diff = diff_text[:60000]
        return (
            f"PR: {pr.owner}/{pr.repo}#{pr.number}\n"
            f"Title: {pr.title}\n"
            f"Changed files JSON:\n{json.dumps(files_payload, ensure_ascii=False)}\n\n"
            "请基于 diff 审查 correctness、security、performance、maintainability、test 问题。\n"
            "输出 JSON 数组，每个对象字段必须是：file_path,line_no,severity,category,title,"
            "description,suggestion,confidence。\n"
            "severity 只能是 low/medium/high/critical；category 只能是 correctness/"
            "maintainability/performance/security/test。\n"
            "如果没有明确问题，输出 []。\n\n"
            f"Diff:\n{limited_diff}"
        )


def build_provider(
    provider_name: str,
    api_key: str | None,
    base_url: str,
    default_model: str,
) -> LLMProvider:
    if provider_name == "mock":
        return MockProvider()
    if provider_name == "openai":
        return OpenAICompatibleProvider(api_key, base_url, default_model)
    raise ValueError("REPOGUARDIAN_PROVIDER 只支持 mock 或 openai")

