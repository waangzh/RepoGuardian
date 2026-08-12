"""基于显式版本外价格配置计算单次模型调用成本。"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


def calculate_cost_microusd(
    pricing_json: str,
    *,
    provider: str,
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
    cached_input_tokens: int | None,
) -> int | None:
    try:
        catalog = json.loads(pricing_json or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(catalog, dict):
        return None
    rates = _find_rates(catalog, provider, model)
    if rates is None or input_tokens is None or output_tokens is None:
        return None
    try:
        input_rate = _rate(rates, "input")
        output_rate = _rate(rates, "output")
        cached_rate = _rate(rates, "cached_input", default=input_rate)
    except (InvalidOperation, TypeError, ValueError):
        return None
    cached = min(cached_input_tokens or 0, input_tokens)
    uncached = input_tokens - cached
    # USD/1M tokens converted to micro-USD cancels the one-million factor.
    cost = Decimal(uncached) * input_rate + Decimal(cached) * cached_rate
    cost += Decimal(output_tokens) * output_rate
    return int(cost.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _find_rates(catalog: dict[str, Any], provider: str, model: str) -> dict[str, Any] | None:
    for key in (f"{provider}:{model}", model, f"{provider}:*"):
        value = catalog.get(key)
        if isinstance(value, dict):
            return value
    return None


def _rate(rates: dict[str, Any], name: str, *, default: Decimal | None = None) -> Decimal:
    value = rates.get(name)
    if value is None and default is not None:
        return default
    rate = Decimal(str(value))
    if rate < 0:
        raise ValueError("model price must not be negative")
    return rate
