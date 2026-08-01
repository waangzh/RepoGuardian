"""可复用 Review Unit、Patch 和 Validation 的稳定 fingerprint。"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable


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
) -> str:
    return stable_hash({
        "base_sha": base_sha,
        "head_sha": head_sha,
        "normalized_unit_diff": normalize_unified_diff(normalized_unit_diff),
        "primary_files": sorted(primary_files),
        "related_files": sorted(related_files),
        "rule_ids": sorted(rule_ids),
        "rule_version": rule_version,
        "prompt_version": prompt_version,
        "tool_schema_version": tool_schema_version,
        "planner_version": planner_version,
        "review_policy_version": review_policy_version,
        "model": model,
        "provider": provider,
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


def validation_fingerprint(
    *, patch_hash: str, backend: str, validation_profile: str, environment_fingerprint: str
) -> str:
    return stable_hash({
        "patch_hash": patch_hash,
        "backend": backend,
        "validation_profile": validation_profile,
        "environment_fingerprint": environment_fingerprint,
    })
