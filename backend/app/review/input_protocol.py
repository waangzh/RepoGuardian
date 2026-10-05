"""Unit 输入协议；旧适配只允许在明确启用的调用范围内使用。"""

from contextvars import ContextVar

CANONICAL_UNIT_INPUT_PROTOCOL = "canonical-evidence-v3"
legacy_unit_input_allowed: ContextVar[bool] = ContextVar("legacy_unit_input_allowed", default=False)
