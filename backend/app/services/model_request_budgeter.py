"""发送同一份不可变请求；窗口检查与累计预算预留分别执行。"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
from typing import Any

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
class PreparedModelRequest:
    model: str
    operation: str
    system: str
    prompt: str
    output_tokens: int
    extra_body_json: str = "{}"

    def estimate(self, profile: ModelRequestProfile) -> dict[str, Any]:
        envelope = json.dumps({
            "model": self.model, "messages": [
                {"role": "system", "content": self.system},
                {"role": "user", "content": self.prompt}],
            "response_format": {"type": "json_object"}, "temperature": 0.1,
            "max_tokens": self.output_tokens, "extra_body": json.loads(self.extra_body_json),
        }, ensure_ascii=False, separators=(",", ":"))
        try:
            import tiktoken
            encoding = tiktoken.encoding_for_model(self.model)
            count = len(encoding.encode(envelope, disallowed_special=()))
            method = f"tiktoken:{encoding.name}:request-envelope"
        except (KeyError, ImportError, OSError):
            count = len(envelope.encode("utf-8"))
            method = "conservative_utf8_bytes:request-envelope"
        metadata = {
            "estimated_input_tokens": count, "count_method": method,
            "count_is_exact": False, "profile": profile.model_dump(mode="json"),
            "request_hash": hashlib.sha256(envelope.encode("utf-8")).hexdigest(),
            "prompt_chars": len(self.prompt),
            "reserved_tokens": count + self.output_tokens,
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


request_budget_reserver: ContextVar[Any] = ContextVar("request_budget_reserver", default=None)
unit_budget_snapshot: ContextVar[Any] = ContextVar("unit_budget_snapshot", default=None)


class UnitRequestLedger:
    def __init__(self, budget: ExecutionBudget):
        self.budget = budget
        self.reserved_tokens = 0
        self.last_reservation = 0
        self.settled: set[str] = set()

    async def reserve(self, metadata: dict[str, Any]) -> None:
        amount = metadata["reserved_tokens"]
        if not self.budget.can_consume(model_calls=1, token_usage=amount):
            raise RequestAdmissionError("unit_request_budget_exhausted: " + json.dumps({
                "requested_tokens": amount, "remaining_tokens": max(
                    0, self.budget.max_token_usage - self.budget.token_usage),
                "remaining_calls": max(0, self.budget.max_model_calls - self.budget.model_calls),
            }))
        # No await between check and consume: one Unit ledger has one writer.
        self.budget = self.budget.consume(model_calls=1, token_usage=amount)
        self.reserved_tokens += amount
        self.last_reservation = amount

    def settle(self, usage: ModelUsage | None) -> None:
        if usage is None or usage.id in self.settled:
            return
        self.settled.add(usage.id)
        if usage.actual_total_tokens is not None:
            correction = max(0, usage.actual_total_tokens - self.last_reservation)
            self.budget = self.budget.model_copy(update={
                "token_usage": self.budget.token_usage + correction})

    @contextmanager
    def activate(self):
        token = request_budget_reserver.set(self.reserve)
        try:
            yield self
        finally:
            request_budget_reserver.reset(token)
