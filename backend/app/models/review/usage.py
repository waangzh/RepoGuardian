"""模型用量与调用结果模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Generic, Iterator, Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    model_validator,
)

from app.models.review.common import ModelCallValue, ReviewUnitComplexity


class ModelUsage(BaseModel):
    """一次模型调用的资源观测；实际用量用于补记超过请求预留的消耗。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex)
    provider: str
    model: str
    operation: str
    review_unit_id: str | None = None
    unit_complexity: "ReviewUnitComplexity | None" = None
    accounted_tokens_estimate: int | None = Field(default=None, ge=0)
    estimated_input_tokens: int = Field(default=0, ge=0)
    max_output_tokens: int = Field(default=0, ge=0)
    actual_input_tokens: int | None = Field(default=None, ge=0)
    actual_output_tokens: int | None = Field(default=None, ge=0)
    actual_total_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    reasoning_output_tokens: int | None = Field(default=None, ge=0)
    latency_ms: int = Field(ge=0)
    cost_microusd: int | None = Field(default=None, ge=0)
    usage_available: bool = False
    accounting_source: Literal["actual", "missing"] = "missing"
    response_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="before")
    @classmethod
    def discard_serialized_computed_fields(cls, value: Any) -> Any:
        """允许 model_dump 产物安全地从图状态或快照中重建。"""
        if isinstance(value, dict) and "estimation_delta_tokens" in value:
            value = dict(value)
            value.pop("estimation_delta_tokens")
        return value

    @computed_field
    @property
    def estimation_delta_tokens(self) -> int | None:
        if self.accounted_tokens_estimate is None or self.actual_total_tokens is None:
            return None
        return self.accounted_tokens_estimate - self.actual_total_tokens


class ModelUsageStats(BaseModel):
    calls: int = Field(default=0, ge=0)
    usage_available_calls: int = Field(default=0, ge=0)
    usage_missing_calls: int = Field(default=0, ge=0)
    usage_coverage_rate: float = Field(default=0.0, ge=0, le=1)
    actual_input_tokens: int = Field(default=0, ge=0)
    actual_output_tokens: int = Field(default=0, ge=0)
    actual_total_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    reasoning_output_tokens: int = Field(default=0, ge=0)
    accounted_tokens_estimate: int = Field(default=0, ge=0)
    estimation_delta_tokens: int = 0
    cost_microusd: int = Field(default=0, ge=0)
    cost_available_calls: int = Field(default=0, ge=0)
    input_tokens_p50: int | None = Field(default=None, ge=0)
    input_tokens_p95: int | None = Field(default=None, ge=0)
    output_tokens_p50: int | None = Field(default=None, ge=0)
    output_tokens_p95: int | None = Field(default=None, ge=0)
    latency_ms_p50: int | None = Field(default=None, ge=0)
    latency_ms_p95: int | None = Field(default=None, ge=0)


class ModelUsageGroup(BaseModel):
    key: str
    stats: ModelUsageStats


class ModelUsageSummary(BaseModel):
    overall: ModelUsageStats = Field(default_factory=ModelUsageStats)
    by_operation: list[ModelUsageGroup] = Field(default_factory=list)
    by_unit_complexity: list[ModelUsageGroup] = Field(default_factory=list)
    by_provider: list[ModelUsageGroup] = Field(default_factory=list)


@dataclass(frozen=True)
class ModelCallResult(Generic[ModelCallValue]):
    """Provider 业务结果及其对应的资源观测。"""

    value: ModelCallValue
    usage: ModelUsage

    def __getattr__(self, name: str) -> Any:
        return getattr(self.value, name)

    def __iter__(self) -> Iterator[Any]:
        return iter(self.value)  # type: ignore[arg-type]

    def __len__(self) -> int:
        return len(self.value)  # type: ignore[arg-type]

    def __getitem__(self, key: Any) -> Any:
        return self.value[key]  # type: ignore[index]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ModelCallResult):
            return self.value == other.value and self.usage == other.usage
        return self.value == other
