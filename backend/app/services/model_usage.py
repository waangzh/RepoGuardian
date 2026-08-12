"""模型资源观测的兼容解包、标注与确定性聚合。"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterable, TypeVar

from app.models.review import (
    ModelCallResult,
    ModelUsage,
    ModelUsageGroup,
    ModelUsageStats,
    ModelUsageSummary,
    ReviewUnitComplexity,
)


T = TypeVar("T")


def unpack_model_call(value: T | ModelCallResult[T]) -> tuple[T, ModelUsage | None]:
    """兼容尚未迁移的测试/第三方 Provider，其旧返回值没有 usage。"""
    if isinstance(value, ModelCallResult):
        return value.value, value.usage
    return value, None


def annotate_usage(
    usage: ModelUsage | None,
    *,
    accounted_tokens_estimate: int | None = None,
    review_unit_id: str | None = None,
    unit_complexity: ReviewUnitComplexity | None = None,
) -> ModelUsage | None:
    if usage is None:
        return None
    return usage.model_copy(update={
        "accounted_tokens_estimate": accounted_tokens_estimate,
        "review_unit_id": review_unit_id,
        "unit_complexity": unit_complexity,
    })


def append_usage(raw: Iterable[dict[str, Any] | ModelUsage], usage: ModelUsage | None) -> list[dict[str, Any]]:
    items = [
        item.model_dump(mode="json") if isinstance(item, ModelUsage) else dict(item)
        for item in raw
    ]
    if usage is not None:
        items.append(usage.model_dump(mode="json"))
    return items


def summarize_model_usage(usages: Iterable[ModelUsage]) -> ModelUsageSummary:
    items = list(usages)
    return ModelUsageSummary(
        overall=_stats(items),
        by_operation=_groups(items, lambda item: item.operation),
        by_unit_complexity=_groups(
            items,
            lambda item: item.unit_complexity.value if item.unit_complexity else "task",
        ),
        by_provider=_groups(items, lambda item: item.provider),
    )


def _groups(items: list[ModelUsage], key_fn: Any) -> list[ModelUsageGroup]:
    grouped: dict[str, list[ModelUsage]] = defaultdict(list)
    for item in items:
        grouped[str(key_fn(item))].append(item)
    return [
        ModelUsageGroup(key=key, stats=_stats(grouped[key]))
        for key in sorted(grouped)
    ]


def _stats(items: list[ModelUsage]) -> ModelUsageStats:
    available = [item for item in items if item.usage_available]
    with_cost = [item for item in items if item.cost_microusd is not None]
    actual_input = [item.actual_input_tokens for item in available if item.actual_input_tokens is not None]
    actual_output = [item.actual_output_tokens for item in available if item.actual_output_tokens is not None]
    deltas = [item.estimation_delta_tokens for item in available if item.estimation_delta_tokens is not None]
    return ModelUsageStats(
        calls=len(items),
        usage_available_calls=len(available),
        usage_missing_calls=len(items) - len(available),
        usage_coverage_rate=(len(available) / len(items)) if items else 0.0,
        actual_input_tokens=sum(item.actual_input_tokens or 0 for item in available),
        actual_output_tokens=sum(item.actual_output_tokens or 0 for item in available),
        actual_total_tokens=sum(item.actual_total_tokens or 0 for item in available),
        cached_input_tokens=sum(item.cached_input_tokens or 0 for item in available),
        reasoning_output_tokens=sum(item.reasoning_output_tokens or 0 for item in available),
        accounted_tokens_estimate=sum(item.accounted_tokens_estimate or 0 for item in items),
        estimation_delta_tokens=sum(deltas),
        cost_microusd=sum(item.cost_microusd or 0 for item in with_cost),
        cost_available_calls=len(with_cost),
        input_tokens_p50=_percentile(actual_input, 0.50),
        input_tokens_p95=_percentile(actual_input, 0.95),
        output_tokens_p50=_percentile(actual_output, 0.50),
        output_tokens_p95=_percentile(actual_output, 0.95),
        latency_ms_p50=_percentile([item.latency_ms for item in items], 0.50),
        latency_ms_p95=_percentile([item.latency_ms for item in items], 0.95),
    )


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]
