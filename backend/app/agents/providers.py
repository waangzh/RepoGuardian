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
                            title="Mock review notice",
                            description=(
                                "The backend is using MockProvider. This validates the review "
                                "pipeline only and does not represent a real code issue."
                            ),
                            suggestion=(
                                "Set REPOGUARDIAN_PROVIDER=openai or deepseek and configure "
                                "OPENAI_API_KEY to enable real LLM review."
                            ),
                            confidence=0.2,
                        )
                    ]
        return []


class OpenAICompatibleProvider(LLMProvider):
    _CONFIDENCE_LABELS = {
        "very_low": 0.15,
        "low": 0.35,
        "medium": 0.65,
        "high": 0.85,
        "very_high": 0.95,
        "critical": 0.9,
    }

    def __init__(
        self,
        api_key: str | None,
        base_url: str,
        default_model: str,
        disable_thinking: bool = False,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model
        self._disable_thinking = disable_thinking
        self._issue_adapter = TypeAdapter(list[ReviewIssue])

    async def review(
        self,
        pr: PullRequestInfo,
        changed_files: list[ChangedFile],
        diff_text: str,
        model: str | None,
    ) -> list[ReviewIssue]:
        if not self._api_key:
            raise LLMProviderError("OPENAI_API_KEY is required for real LLM review")

        payload = self._build_request_payload(pr, changed_files, diff_text, model)
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                f"{self._base_url}/chat/completions", headers=headers, json=payload
            )

        if response.status_code >= 400:
            raise LLMProviderError(f"LLM request failed: {response.status_code} {response.text[:500]}")

        response_payload = response.json()
        content = self._extract_message_content(response_payload)
        return self._parse_issues(content)

    def _build_request_payload(
        self,
        pr: PullRequestInfo,
        changed_files: list[ChangedFile],
        diff_text: str,
        model: str | None,
    ) -> dict[str, Any]:
        prompt = self._build_prompt(pr, changed_files, diff_text)
        payload: dict[str, Any] = {
            "model": model or self._default_model,
            "temperature": 0.1,
            "max_tokens": 4096,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a strict code review agent. Report only issues with "
                        "clear evidence. Return valid json only. Do not use Markdown."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        }
        if self._disable_thinking:
            payload["thinking"] = {"type": "disabled"}
        return payload

    @staticmethod
    def _extract_message_content(payload: dict[str, Any]) -> str:
        choices = payload.get("choices")
        if not choices:
            raise LLMProviderError(f"LLM response missing choices: {json.dumps(payload)[:500]}")

        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            raise LLMProviderError(f"LLM response missing message: {json.dumps(payload)[:500]}")

        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content

        reasoning_content = message.get("reasoning_content")
        if isinstance(reasoning_content, str) and reasoning_content.strip():
            raise LLMProviderError(
                "LLM response only contained reasoning_content and no final JSON content. "
                "For DeepSeek, use REPOGUARDIAN_PROVIDER=deepseek so thinking is disabled."
            )

        raise LLMProviderError(f"LLM response missing content: {json.dumps(payload)[:500]}")

    def _parse_issues(self, content: str) -> list[ReviewIssue]:
        try:
            raw = json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"\{[\s\S]*\}|\[[\s\S]*\]", content)
            if not match:
                raise LLMProviderError("LLM output is not valid JSON")
            raw = json.loads(match.group(0))

        raw_issues = self._extract_raw_issues(raw)
        normalized_issues = [self._normalize_issue(issue) for issue in raw_issues]

        try:
            return self._issue_adapter.validate_python(normalized_issues)
        except ValidationError as exc:
            raise LLMProviderError(f"LLM issue schema validation failed: {exc}") from exc

    @staticmethod
    def _extract_raw_issues(raw: Any) -> list[Any]:
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            issues = raw.get("issues")
            if isinstance(issues, list):
                return issues
            if issues is None:
                return []
        raise LLMProviderError("LLM output must be a JSON object with an issues array")

    @classmethod
    def _normalize_issue(cls, issue: Any) -> Any:
        if not isinstance(issue, dict):
            return issue
        normalized = dict(issue)
        normalized["confidence"] = cls._normalize_confidence(normalized.get("confidence"))
        return normalized

    @classmethod
    def _normalize_confidence(cls, value: Any) -> float:
        if value is None:
            return 0.5
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        if isinstance(value, (int, float)):
            return cls._clamp_confidence(float(value))
        if isinstance(value, str):
            normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
            if normalized in cls._CONFIDENCE_LABELS:
                return cls._CONFIDENCE_LABELS[normalized]
            if normalized.endswith("%"):
                return cls._clamp_confidence(float(normalized[:-1]) / 100)
            try:
                return cls._clamp_confidence(float(normalized))
            except ValueError:
                return 0.5
        return 0.5

    @staticmethod
    def _clamp_confidence(value: float) -> float:
        if value > 1 and value <= 100:
            value = value / 100
        return min(max(value, 0.0), 1.0)

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
            "Review the diff for correctness, security, performance, maintainability, "
            "and test coverage issues. Return Chinese text for title, description, "
            "and suggestion when possible.\n"
            "Return valid json as a single JSON object with this exact shape:\n"
            "{\"issues\":[{\"file_path\":\"path/to/file\",\"line_no\":1,"
            "\"severity\":\"high\",\"category\":\"correctness\","
            "\"title\":\"问题标题\",\"description\":\"问题说明\","
            "\"suggestion\":\"修复建议\",\"confidence\":0.85}]}\n"
            "The confidence field must be a number between 0 and 1. Do not use strings "
            "such as high, medium, or low for confidence.\n"
            "severity must be one of: low, medium, high, critical. "
            "category must be one of: correctness, maintainability, performance, security, test.\n"
            "If there is no clear issue, return {\"issues\":[]}.\n\n"
            f"Diff:\n{limited_diff}"
        )


def build_provider(
    provider_name: str,
    api_key: str | None,
    base_url: str,
    default_model: str,
) -> LLMProvider:
    normalized_provider = provider_name.strip().lower()
    if normalized_provider == "mock":
        return MockProvider()
    if normalized_provider in {"openai", "deepseek", "openai-compatible"}:
        disable_thinking = normalized_provider == "deepseek" or "deepseek.com" in base_url.lower()
        return OpenAICompatibleProvider(api_key, base_url, default_model, disable_thinking)
    raise ValueError(
        "REPOGUARDIAN_PROVIDER must be one of: mock, openai, deepseek, openai-compatible"
    )

