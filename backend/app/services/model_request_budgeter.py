"""发送同一份不可变请求；窗口检查与累计预算预留分别执行。"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import lru_cache
import hashlib
import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.review import ExecutionBudget, ModelUsage


class RequestAdmissionError(ValueError):
    pass


class ModelRequestProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    context_window: int = Field(default=16_384, ge=1)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    safety_margin_tokens: int = Field(default=512, ge=0)
    input_output_shared: bool = True

    @model_validator(mode="after")
    def usable_window(self):
        if self.safety_margin_tokens >= self.context_window:
            raise ValueError("safety margin must be smaller than context window")
        return self


@dataclass(frozen=True)
class ResolvedModelProfile:
    profile: ModelRequestProfile
    source: str


# Exact names and official endpoint only; compatible proxies may impose different limits.
# https://developers.openai.com/api/docs/models/gpt-4.1-mini
KNOWN_MODEL_PROFILES = {
    "gpt-4.1-mini": ModelRequestProfile(context_window=1_047_576, max_output_tokens=32_768),
    "gpt-4.1-mini-2025-04-14": ModelRequestProfile(context_window=1_047_576, max_output_tokens=32_768),
}
# https://api-docs.deepseek.com/zh-cn/quick_start/pricing/
# The docs specify 1M without an exact integer; use 1,000,000 conservatively.
# Chat Completions defines 384K output as 393216, shared with the input window.
DEEPSEEK_MODEL_PROFILES = {
    model: ModelRequestProfile(context_window=1_000_000, max_output_tokens=393_216)
    for model in ("deepseek-flash", "deepseek-v4-pro", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp")
}
CONTEXT_BUDGET_VERSION = "model-context-budget-v3-settled-usage"
SERIALIZATION_CHAR_LIMIT = 2_000_000


def resolve_model_profile(model: str, provider: str, base_url: str,
                          default: ModelRequestProfile | None = None,
                          overrides: dict[str, ModelRequestProfile] | None = None) -> ResolvedModelProfile:
    if model in (overrides or {}):
        return ResolvedModelProfile(overrides[model], "user_model_override")
    if default is not None:
        return ResolvedModelProfile(default, "user_default_override")
    endpoint = urlsplit(base_url)
    if endpoint.scheme == "https" and not endpoint.query and not endpoint.fragment:
        registry = {}
        if (provider in {"openai", "openai-compatible"} and endpoint.netloc == "api.openai.com"
                and endpoint.path.rstrip("/") == "/v1"):
            registry = KNOWN_MODEL_PROFILES
        elif (provider in {"openai", "deepseek", "openai-compatible"} and endpoint.netloc == "api.deepseek.com"
              and endpoint.path.rstrip("/") in {"", "/v1"}):
            registry = DEEPSEEK_MODEL_PROFILES
        if model in registry:
            return ResolvedModelProfile(registry[model], "known_model_registry")
    return ResolvedModelProfile(ModelRequestProfile(), "generic_fallback")


@lru_cache(maxsize=128)
def _encoding(model: str):
    try:
        import tiktoken
        return tiktoken.encoding_for_model(model)
    except (KeyError, ImportError, OSError):
        return None


@lru_cache(maxsize=1)
def _deepseek_encoding():
    # 仅加载用户提供的静态词表，不执行模型目录中的 Python，也不联网下载。
    try:
        from tokenizers import Tokenizer
        path = Path(__file__).resolve().parents[1] / "tools/deepseek_v4_tokenizer/tokenizer.json"
        tokenizer = Tokenizer.from_file(str(path))
        tokenizer.no_truncation()
        tokenizer.no_padding()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return tokenizer, digest
    except Exception as exc:
        # tokenizers 对损坏的 JSON 使用原生 Exception，加载失败仍显式保守回退。
        logging.getLogger("RepoGuardian.LLM").warning("DeepSeek tokenizer 不可用：%s", type(exc).__name__)
        return None


def estimate_tokens(text: str, model: str) -> tuple[int, str]:
    if model in DEEPSEEK_MODEL_PROFILES:
        local = _deepseek_encoding()
        if local is not None:
            tokenizer, digest = local
            count = len(tokenizer.encode(text, add_special_tokens=False).ids)
            return count, f"deepseek_v4:{digest}:request-envelope"
    encoding = _encoding(model)
    if encoding is not None:
        return len(encoding.encode(text, disallowed_special=())), f"tiktoken:{encoding.name}:request-envelope"
    return len(text.encode("utf-8")), "conservative_utf8_bytes:request-envelope"


@dataclass(frozen=True)
class PreparedModelRequest:
    model: str
    operation: str
    system: str
    prompt: str
    output_tokens: int
    extra_body_json: str = "{}"
    profile_source: str | None = None

    def envelope(self) -> str:
        return json.dumps({
            "model": self.model, "messages": [
                {"role": "system", "content": self.system},
                {"role": "user", "content": self.prompt}],
            "response_format": {"type": "json_object"}, "temperature": 0.1,
            "max_tokens": self.output_tokens, "extra_body": json.loads(self.extra_body_json),
        }, ensure_ascii=False, separators=(",", ":"))

    def estimate(self, profile: ModelRequestProfile) -> dict[str, Any]:
        envelope = self.envelope()
        count, method = estimate_tokens(envelope, self.model)
        metadata = {
            "estimated_input_tokens": count, "count_method": method,
            "count_is_exact": False, "profile": profile.model_dump(mode="json"),
            "request_hash": hashlib.sha256(envelope.encode("utf-8")).hexdigest(),
            "prompt_chars": len(self.prompt),
            "reserved_tokens": count + self.output_tokens,
            "model_profile_source": self.profile_source,
            "context_budget_version": CONTEXT_BUDGET_VERSION,
        }
        reason = None
        if profile.max_input_tokens is not None and count > profile.max_input_tokens:
            reason = "model_input_limit_exceeded"
        elif profile.max_output_tokens is not None and self.output_tokens > profile.max_output_tokens:
            reason = "model_output_limit_exceeded"
        elif (count + (self.output_tokens if profile.input_output_shared else 0)
              + profile.safety_margin_tokens > profile.context_window):
            reason = "model_context_window_exceeded"
        if reason:
            raise RequestAdmissionError(reason + ": " + json.dumps(metadata))
        return metadata


@dataclass(frozen=True)
class ModelContextBudget:
    """Select complete objects against the same envelope used by final admission."""
    request: PreparedModelRequest
    profile: ModelRequestProfile

    @property
    def input_limit(self) -> int:
        window = self.profile.context_window - self.profile.safety_margin_tokens
        if self.profile.input_output_shared:
            window -= self.request.output_tokens
        return min(window, self.profile.max_input_tokens or window)

    def with_prefix(self, prefix: str) -> "ModelContextBudget":
        return replace(self, request=replace(self.request, prompt=prefix))

    def describe(self) -> dict[str, Any]:
        fixed, method = estimate_tokens(self.request.envelope(), self.request.model)
        return {"version": CONTEXT_BUDGET_VERSION, "input_limit_tokens": self.input_limit,
                "fixed_request_tokens": fixed, "available_input_tokens": max(0, self.input_limit - fixed),
                "reserved_output_tokens": self.request.output_tokens, "count_method": method,
                "model_profile_source": self.request.profile_source}

    def fits(self, serialized: str) -> bool:
        if (self.profile.max_output_tokens is not None
                and self.request.output_tokens > self.profile.max_output_tokens):
            return False
        envelope = replace(self.request, prompt=self.request.prompt + serialized).envelope()
        return estimate_tokens(envelope, self.request.model)[0] <= self.input_limit


request_budget_reserver: ContextVar[Any] = ContextVar("request_budget_reserver", default=None)
unit_budget_snapshot: ContextVar[Any] = ContextVar("unit_budget_snapshot", default=None)


class UnitRequestLedger:
    def __init__(self, budget: ExecutionBudget, *, holdback: dict[str, Any] | None = None):
        self.budget = budget
        self.holdback = dict(holdback or {})
        self.rejection: dict[str, Any] | None = None
        self.reserved_tokens = 0
        self.last_reservation = 0
        self.settled: set[str] = set()

    async def reserve(self, metadata: dict[str, Any]) -> None:
        amount = metadata["reserved_tokens"]
        held_tokens = self.holdback.get("token_usage", 0)
        held_calls = self.holdback.get("model_calls", 0)
        if not self.budget.can_consume(model_calls=1 + held_calls, token_usage=amount + held_tokens):
            self.rejection = {
                "requested_tokens": amount, "remaining_tokens": max(
                    0, self.budget.max_token_usage - self.budget.token_usage),
                "remaining_calls": max(0, self.budget.max_model_calls - self.budget.model_calls),
                "holdback": self.holdback,
                "available_tokens": max(0, self.budget.max_token_usage - self.budget.token_usage - held_tokens),
                "transport_retry": self.reserved_tokens > 0,
            }
            raise RequestAdmissionError("unit_request_budget_exhausted: " + json.dumps(self.rejection))
        # No await between check and consume: one Unit ledger has one writer.
        self.budget = self.budget.consume(model_calls=1, token_usage=amount)
        self.reserved_tokens += amount
        self.last_reservation = amount

    def settle(self, usage: ModelUsage | None) -> None:
        if usage is None or usage.id in self.settled:
            return
        self.settled.add(usage.id)
        if usage.actual_total_tokens is not None:
            # 仅结算本次有实测 usage 的尝试；未知的前次重试仍保留完整预留。
            correction = usage.actual_total_tokens - self.last_reservation
            self.budget = self.budget.model_copy(update={
                "token_usage": self.budget.token_usage + correction})
            if self.budget.token_usage > self.budget.max_token_usage:
                self.rejection = {"reason": "unit_request_budget_overrun",
                    "actual_accounted_tokens": self.budget.token_usage,
                    "max_token_usage": self.budget.max_token_usage}
                error = RequestAdmissionError("unit_request_budget_overrun: " + json.dumps(self.rejection))
                error.usage = usage
                raise error

    @contextmanager
    def activate(self):
        token = request_budget_reserver.set(self.reserve)
        try:
            yield self
        finally:
            request_budget_reserver.reset(token)
