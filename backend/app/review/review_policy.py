"""V2 审查协议的集中策略，避免 Provider/执行器各自维护输出默认值。"""

from __future__ import annotations

from typing import Any


REVIEW_STEP_PROTOCOL_VERSION = "review-step-v1"
REVIEW_STEP_OUTPUT_TOKENS = 4_096
REVIEW_STEP_MAX_NEXT_NEEDS = 1


def input_composition(record_input: dict[str, Any]) -> dict[str, int]:
    """只统计输入组成，正文仍由现有证据存储和容量检查负责。"""
    def chars(value: Any) -> int:
        if isinstance(value, str):
            return len(value)
        if isinstance(value, list):
            return sum(chars(item) for item in value)
        if isinstance(value, dict):
            return sum(chars(key) + chars(item) for key, item in value.items())
        return 0

    return {
        "diff_chars": chars(record_input.get("diff_evidence") or record_input.get("changed_files")),
        "evidence_chars": chars(record_input.get("readonly_context") or record_input.get("evidence")),
        "working_memory_chars": chars(record_input.get("working_memory")),
        "manifest_chars": chars(record_input.get("diff_manifest") or record_input.get("coverage_ledger")),
        "pr_intent_chars": chars(record_input.get("pr_intent")),
    }
