"""可复用 Review Unit、Patch 和 Validation 的稳定 fingerprint。"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable
from app.services.model_request_budgeter import CONTEXT_BUDGET_VERSION

DIAGNOSIS_INPUT_VERSION = "provider-memory-v16-model-context-budget-v2-metadata-prompt-verifier-v2-evidence-store-v1-active-context-v1-diff-manifest-v1-coverage-ledger-v1-unit-diff-worksets-v1-canonical-evidence-v3"


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
        "unit_input_mode": settings.repoguardian_unit_input_mode,
    })


def patch_fingerprint(
    *, head_sha: str, issue_evidence_hash: str, unified_diff: str, patch_policy_version: str
) -> tuple[str, str]:
    diff_hash = hashlib.sha256(normalize_unified_diff(unified_diff).encode("utf-8")).hexdigest()
    return stable_hash({
        "head_sha": head_sha,
        "issue_evidence_hash": issue_evidence_hash,
        "unified_diff_hash": diff_hash,
        "patch_policy_version": patch_policy_version,
    }), diff_hash


def unit_execution_fingerprint(unit_fp: str, state: dict, provider: Any, input_mode: str) -> str:
    """即使 plan 来自旧 checkpoint，也绑定本次实际模型、配置和输入版本。"""
    from app.core.config import settings
    from app.services.review_input_context import build_pr_intent

    model = state.get("model") or getattr(provider, "_default_model", settings.repoguardian_model)
    profiles = getattr(provider, "_model_profiles", settings.repoguardian_model_request_profiles)
    profile = profiles.get(model, getattr(provider, "_request_profile", settings.repoguardian_model_request_profile))
    resolver = getattr(provider, "resolve_request_profile", None)
    resolved = resolver(model) if callable(resolver) else settings.resolve_model_profile(model)
    if callable(resolver):
        profile = resolved.profile
    return stable_hash({
        "unit_fp": unit_fp, "input_version": DIAGNOSIS_INPUT_VERSION, "input_mode": input_mode,
        "head_sha": state.get("head_sha"), "base_sha": state.get("diff_base_sha") or state.get("base_sha"),
        "pr_intent": build_pr_intent(state.get("pr_info"))["intent_hash"], "model": model,
        "provider": getattr(provider, "_provider_name", settings.repoguardian_provider),
        "provider_type": f"{type(provider).__module__}.{type(provider).__qualname__}",
        "endpoint": getattr(provider, "_base_url", settings.openai_base_url),
        "profile": profile.model_dump(mode="json"), "prompt": settings.repoguardian_prompt_version,
        "context_budget_version": CONTEXT_BUDGET_VERSION, "model_profile_source": resolved.source,
        "rules": settings.repoguardian_rule_version, "tools": settings.repoguardian_tool_schema_version,
        "policy": settings.repoguardian_review_policy_version,
    })


def validation_fingerprint(
    *, patch_hash: str, backend: str, validation_profile: str, environment_fingerprint: str
) -> str:
    return stable_hash({
        "patch_hash": patch_hash,
        "backend": backend,
        "validation_profile": validation_profile,
        "environment_fingerprint": environment_fingerprint,
    })
