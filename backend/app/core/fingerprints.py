"""可复用 Review Unit、Patch 和 Validation 的稳定 fingerprint。"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable
from app.llm.model_request_budgeter import CONTEXT_BUDGET_VERSION

DIAGNOSIS_INPUT_VERSION = "provider-memory-v24-followup-evidence-closure-target-ids-output-repair-bound-contract-closure-progress-gated-review-isolated-next-need-python-branch-claims-provenance-record-completion-settled-budget-path-counterevidence-evidence-store-v1-active-context-v1-diff-manifest-v1-coverage-ledger-v1-unit-diff-worksets-v1-canonical-evidence-v3"


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_unified_diff(diff: str) -> str:
    """统一换行和行尾空白；保留内容顺序及有语义的行首空白。"""
    text = diff.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+$", "", line) for line in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + ("\n" if lines else "")


def unit_fingerprint(
    *,
    base_sha: str,
    head_sha: str,
    normalized_unit_diff: str,
    primary_files: Iterable[str],
    related_files: Iterable[str],
    rule_ids: Iterable[str],
    rule_version: str,
    prompt_version: str,
    tool_schema_version: str,
    planner_version: str,
    review_policy_version: str,
    model: str,
    provider: str,
    pr_intent_hash: str = "",
) -> str:
    from app.core.config import settings

    return stable_hash({
        "base_sha": base_sha,
        "head_sha": head_sha,
        "normalized_unit_diff": normalize_unified_diff(normalized_unit_diff),
        "primary_files": sorted(primary_files),
        "related_files": sorted(related_files),
        "rule_ids": sorted(rule_ids),
        "rule_version": rule_version,
        "prompt_version": prompt_version,
        "diagnosis_input_version": DIAGNOSIS_INPUT_VERSION,
        "request_profile": settings.resolve_model_profile(model, provider).profile.model_dump(mode="json"),
        "model_profile_source": settings.resolve_model_profile(model, provider).source,
        "context_budget_version": CONTEXT_BUDGET_VERSION,
        "tool_schema_version": tool_schema_version,
        "planner_version": planner_version,
        "review_policy_version": review_policy_version,
        "model": model,
        "provider": provider,
        "pr_intent_hash": pr_intent_hash,
    })
