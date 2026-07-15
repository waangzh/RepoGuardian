import json
import logging
import re
import time
from abc import ABC, abstractmethod
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import TypeAdapter, ValidationError

from app.graph.policies import ALLOWED_ACTIONS_BY_PHASE, get_phase
from app.models.review import AgentAction, ChangedFile, PatchResult, PullRequestInfo, ReviewIssue

logger = logging.getLogger("RepoGuardian.LLM")


class LLMProviderError(RuntimeError):
    pass


class LLMProvider(ABC):
    @abstractmethod
    async def decide(self, state: dict[str, Any], model: str | None) -> AgentAction:
        raise NotImplementedError

    @abstractmethod
    async def review(
        self,
        pr: PullRequestInfo,
        changed_files: list[ChangedFile],
        diff_text: str,
        model: str | None,
    ) -> list[ReviewIssue]:
        raise NotImplementedError

    @abstractmethod
    async def generate_patch(
        self,
        state: dict[str, Any],
        model: str | None,
    ) -> list[PatchResult]:
        raise NotImplementedError


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
        self._patch_adapter = TypeAdapter(list[PatchResult])

    async def decide(self, state: dict[str, Any], model: str | None) -> AgentAction:
        if not self._api_key:
            raise LLMProviderError("OPENAI_API_KEY is required for real agent decisions")

        logger.info("🌐 [LLM决策] 调用 API，模型=%s ...", model or self._default_model)
        prompt = self._build_decision_prompt(state)
        t0 = time.monotonic()
        content = await self._request_json_content(
            prompt=prompt,
            model=model,
            system=(
                "You are the planner for a code review and auto-fix agent. "
                "Return valid JSON only. Choose exactly one next action."
            ),
            max_tokens=1200,
        )
        elapsed = time.monotonic() - t0
        logger.info("🌐 [LLM决策] API 响应 %.2f 秒，长度=%d 字符", elapsed, len(content))
        try:
            raw = self._load_json(content)
            return AgentAction.model_validate(raw)
        except ValidationError as exc:
            raise LLMProviderError(f"Agent action schema validation failed: {exc}") from exc

    async def review(
        self,
        pr: PullRequestInfo,
        changed_files: list[ChangedFile],
        diff_text: str,
        model: str | None,
    ) -> list[ReviewIssue]:
        if not self._api_key:
            raise LLMProviderError("OPENAI_API_KEY is required for real LLM review")

        logger.info("🌐 [LLM审查] 调用 API，模型=%s，%d 个变更文件，diff=%d 字符 ...",
                     model or self._default_model, len(changed_files), len(diff_text))
        prompt = self._build_prompt(pr, changed_files, diff_text)
        t0 = time.monotonic()
        content = await self._request_json_content(
            prompt=prompt,
            model=model,
            system=(
                "You are a strict code review agent. Report only issues with "
                "clear evidence. Return valid json only. Do not use Markdown."
            ),
            max_tokens=4096,
        )
        elapsed = time.monotonic() - t0
        issues = self._parse_issues(content)
        logger.info("🌐 [LLM审查] API 响应 %.2f 秒，发现 %d 个问题", elapsed, len(issues))
        return issues

    async def generate_patch(
        self,
        state: dict[str, Any],
        model: str | None,
    ) -> list[PatchResult]:
        if not self._api_key:
            raise LLMProviderError("OPENAI_API_KEY is required for real patch generation")

        review_issues = state.get("review_issues") or []
        target_ids = (state.get("next_action") or {}).get("target_issue_ids", [])
        logger.info("🌐 [LLM补丁] 调用 API，模型=%s，候选问题=%d，目标=%s ...",
                     model or self._default_model, len(review_issues), target_ids or "全部可自动修复")
        t0 = time.monotonic()
        content = await self._request_json_content(
            prompt=self._build_patch_prompt(state),
            model=model,
            system=(
                "You generate minimal unified diffs for clear code review issues. "
                "Return valid JSON only. Do not use Markdown."
            ),
            max_tokens=4096,
        )
        elapsed = time.monotonic() - t0
        raw = self._load_json(content)
        patches = raw.get("patches", raw) if isinstance(raw, dict) else raw
        try:
            result = self._patch_adapter.validate_python(patches)
            logger.info("🌐 [LLM补丁] API 响应 %.2f 秒，生成 %d 个 patch", elapsed, len(result))
            return result
        except ValidationError as exc:
            raise LLMProviderError(f"Patch schema validation failed: {exc}") from exc

    async def _request_json_content(
        self,
        prompt: str,
        model: str | None,
        system: str,
        max_tokens: int,
    ) -> str:
        chat_model = self._build_chat_model(model, max_tokens)
        try:
            response = await chat_model.ainvoke([
                SystemMessage(content=system),
                HumanMessage(content=prompt),
            ])
        except Exception as exc:
            logger.error("🌐 [LLM] ChatOpenAI 调用失败: %s", type(exc).__name__)
            raise LLMProviderError("LLM request failed") from exc
        return self._extract_message_content(response)

    def _build_chat_model(self, model: str | None, max_tokens: int) -> ChatOpenAI:
        """创建一次调用对应的 ChatOpenAI，保留模型覆写和 JSON 约束。"""
        options: dict[str, Any] = {
            "api_key": self._api_key,
            "base_url": self._base_url,
            "model": model or self._default_model,
            "temperature": 0.1,
            "max_tokens": max_tokens,
            # JSON mode 比 tool calling 更适合 DeepSeek 和通用兼容端点。
            "model_kwargs": {"response_format": {"type": "json_object"}},
        }
        if self._disable_thinking:
            # 非标准字段必须放进 OpenAI SDK 的 extra_body，不能作为普通模型参数。
            options["extra_body"] = {"thinking": {"type": "disabled"}}
        return ChatOpenAI(**options)

    @staticmethod
    def _extract_message_content(message: AIMessage) -> str:
        content = message.content
        if isinstance(content, str) and content.strip():
            return content

        reasoning_content = message.additional_kwargs.get("reasoning_content")
        if isinstance(reasoning_content, str) and reasoning_content.strip():
            raise LLMProviderError(
                "LLM response only contained reasoning_content and no final JSON content. "
                "For DeepSeek, use REPOGUARDIAN_PROVIDER=deepseek so thinking is disabled."
            )

        raise LLMProviderError("LLM response missing string content")

    def _parse_issues(self, content: str) -> list[ReviewIssue]:
        raw = self._load_json(content)

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

    @staticmethod
    def _load_json(content: str) -> Any:
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"\{[\s\S]*\}|\[[\s\S]*\]", content)
            if not match:
                raise LLMProviderError("LLM output is not valid JSON")
            return json.loads(match.group(0))

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
            "\"id\":\"optional-stable-id\",\"auto_fixable\":true,"
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

    @staticmethod
    def _build_decision_prompt(state: dict[str, Any]) -> str:
        phase = get_phase(state)
        compact = {
            "phase": phase.value,
            "changed_files": state.get("changed_files") or [],
            "context_count": len(state.get("context_snippets") or []),
            "static_results": state.get("static_results") or [],
            "review_issues": state.get("review_issues") or [],
            "patches": state.get("patches") or [],
            "test_results": state.get("test_results") or [],
            "execution_budget": state.get("execution_budget") or {},
            "agent_events": state.get("agent_events") or [],
        }
        allowed = ", ".join(
            action.value for action in ALLOWED_ACTIONS_BY_PHASE.get(phase, frozenset())
        )
        return (
            f"Decide the next action for the '{phase.value}' code review phase.\n"
            f"Allowed actions: {allowed}.\n"
            "Return exactly this JSON shape:\n"
            "{\"action\":\"review_code\",\"reason\":\"中文理由\","
            "\"target_issue_ids\":[],\"tool_args\":{}}\n\n"
            f"Current state JSON:\n{json.dumps(compact, ensure_ascii=False)[:50000]}"
        )

    @staticmethod
    def _build_patch_prompt(state: dict[str, Any]) -> str:
        compact = {
            "diff_text": (state.get("diff_text") or "")[:50000],
            "context_snippets": state.get("context_snippets") or [],
            "review_issues": state.get("review_issues") or [],
            "test_results": state.get("test_results") or [],
            "target_issue_ids": (state.get("next_action") or {}).get("target_issue_ids", []),
        }
        return (
            "Generate minimal unified diff patches for clearly auto-fixable issues only. "
            "If no issue can be fixed safely, return {\"patches\":[]}.\n"
            "Return JSON shape:\n"
            "{\"patches\":[{\"issue_id\":\"issue-id\",\"diff_content\":\"diff --git ...\","
            "\"status\":\"generated\"}]}\n\n"
            f"State JSON:\n{json.dumps(compact, ensure_ascii=False)[:60000]}"
        )


def build_provider(
    provider_name: str,
    api_key: str | None,
    base_url: str,
    default_model: str,
) -> LLMProvider:
    """工厂函数：根据配置名创建对应的 LLM Provider 实例。"""
    normalized_provider = provider_name.strip().lower()
    logger.info("🔌 构建 LLM Provider: %s（模型=%s）", normalized_provider, default_model)
    if normalized_provider in {"openai", "deepseek", "openai-compatible"}:
        disable_thinking = normalized_provider == "deepseek" or "deepseek.com" in base_url.lower()
        if disable_thinking:
            logger.info("🔌 检测到 DeepSeek，已禁用 thinking 模式")
        return OpenAICompatibleProvider(api_key, base_url, default_model, disable_thinking)
    raise ValueError(
        "REPOGUARDIAN_PROVIDER must be one of: openai, deepseek, openai-compatible"
    )
