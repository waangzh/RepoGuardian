"""审查内核迁移的离线基线：真实计量对账与隔离的盲审材料。

不导入 Provider、不读取参考答案，不将审查终态当作质量标签。
原始轨迹保留在本地；可提交的 fixture 只保存必要状态和计量身份。
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any


def _identity(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def _tokens(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def reconcile_wire_usage(result: dict[str, Any]) -> dict[str, Any]:
    """按 wire attempt、usage ID 和账本 request/hash 关联，未知不填零。

    这是对已有轨迹的核验，不推断漏失响应的消费，也不改写预算。
    同一 request 的传输重试保留独立 attempt，不按 request_id 去重。
    """
    starts, outcomes, rejected = {}, {}, {}
    errors: list[str] = []
    for event in result.get("model_requests", []):
        key = (event.get("request_id"), event.get("attempt"))
        if not key[0] or not _tokens(key[1]) or key[1] < 1:
            errors.append("invalid_wire_identity")
            continue
        status = event.get("status")
        if status == "admission_rejected":
            if key in rejected:
                errors.append("duplicate_admission_rejection")
            rejected[key] = event
            if (event.get("usage") or {}).get("usage_available"):
                errors.append("admission_rejection_with_usage")
            continue
        if status not in {"started", "completed", "failed", "cancelled"}:
            errors.append("invalid_wire_status")
            continue
        target = starts if status == "started" else outcomes
        if key in target:
            errors.append("duplicate_wire_start" if target is starts else "duplicate_wire_outcome")
        else:
            target[key] = event
    if set(outcomes) - set(starts):
        errors.append("outcome_without_start")
    if set(rejected) & (set(starts) | set(outcomes)):
        errors.append("admission_rejection_with_dispatch")

    usage_ids = []
    unknown = 0
    observed = 0
    attempts = []
    budget = result.get("run_budget") or {}
    ledger = list((budget.get("attempts") or {}).values())
    used_ledger_ids: set[str] = set()
    for key, start in starts.items():
        end = outcomes.get(key, {})
        usage = end.get("usage") or {}
        actual = usage.get("actual_total_tokens")
        if not usage.get("usage_available") or not _tokens(actual):
            actual = None
            unknown += 1
        else:
            observed += actual
            if (not _tokens(usage.get("actual_input_tokens"))
                    or not _tokens(usage.get("actual_output_tokens"))
                    or usage["actual_input_tokens"] + usage["actual_output_tokens"] != actual):
                errors.append("usage_component_mismatch")
        usage_id = usage.get("id")
        if usage_id:
            usage_ids.append(usage_id)
        elif actual is not None:
            errors.append("missing_usage_identity")
        request_hash = (usage.get("response_metadata") or {}).get(
            "request_admission", {}).get("request_hash")
        matches = [item for item in ledger if item.get("request_id") == key[0]
                   and request_hash and item.get("request_hash") == request_hash]
        # 只有请求身份和 request hash 唯一匹配时才建立关联，不能按列表顺序猜测。
        if len(matches) != 1:
            if actual is not None:
                errors.append("ledger_attempt_not_unique")
            ledger_item = {}
        else:
            ledger_item = matches[0]
            identity = ledger_item.get("attempt_id")
            if not identity or identity in used_ledger_ids:
                errors.append("duplicate_ledger_attempt")
            used_ledger_ids.add(identity)
            if actual is not None and (not ledger_item.get("settled")
                                       or ledger_item.get("actual_tokens") != actual):
                errors.append("ledger_usage_mismatch")
            if not ledger_item.get("sent"):
                errors.append("wire_not_marked_sent")
        attempts.append({
            "request_id": key[0], "wire_attempt": key[1],
            "attempt_id": ledger_item.get("attempt_id"), "usage_id": usage_id,
            "operation": start.get("operation"), "status": end.get("status", "missing"),
            "request_hash": request_hash, "actual_total_tokens": actual,
        })
    if len(set(usage_ids)) != len(usage_ids):
        errors.append("duplicate_usage_identity")
    high_level_ids = [(call.get("usage") or {}).get("id")
                      for call in result.get("model_calls", []) if call.get("usage")]
    if not unknown and Counter(high_level_ids) != Counter(usage_ids):
        errors.append("high_level_usage_mismatch")
    sent_ledger = [item for item in ledger if item.get("sent")]
    if not unknown and {item.get("attempt_id") for item in sent_ledger} != used_ledger_ids:
        errors.append("ledger_sent_attempt_mismatch")
    if budget.get("calls_consumed") != len(starts):
        errors.append("ledger_call_count_mismatch")
    if not unknown and budget.get("settled_actual") != observed:
        errors.append("ledger_total_mismatch")
    unreconciled = bool(budget.get("in_flight_reserved") or budget.get("unknown_reserved"))
    incomplete = bool(unknown or unreconciled)
    return {
        "integrity": "inconsistent" if errors else "unknown" if incomplete else "complete",
        "wire_attempts": len(starts), "unknown_usage_attempts": unknown,
        "admission_rejections": len(rejected),
        "unreconciled_reservations": unreconciled,
        "observed_total_tokens": observed,
        "actual_total_tokens": None if incomplete or errors else observed,
        "errors": sorted(set(errors)), "attempts": attempts,
    }


def blind_review_packets(result: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """候选内容和 pipeline 终态分开；包含验证器丢弃项，不读参考答案。

    材料只支持静态语义复核，候选引用自身不是代码事实。完整调用契约
    仍须从冻结 SHA 的源码核验，不能用这个包直接计算 precision/recall。
    """
    packets, mapping = [], []
    seen: set[tuple[str, str]] = set()
    for event in result.get("issue_audit", []):
        issue = event.get("issue")
        if event.get("stage") != "generated" or not isinstance(issue, dict):
            continue
        key = (event.get("review_unit_id"), event.get("issue_id"))
        if key in seen:
            continue
        seen.add(key)
        claim = {name: issue.get(name) for name in (
            "title", "affected_behavior", "failure_scenario", "recommendation",
            "assumptions", "primary_evidence", "supporting_evidence",
        )}
        # 去掉证据解析和发布状态；保留模型主张的源码及坐标供独立定位。
        for field in ("primary_evidence", "supporting_evidence"):
            raw = claim[field]
            if field == "primary_evidence":
                evidence = [raw] if isinstance(raw, dict) else []
            else:
                evidence = raw if isinstance(raw, list) else []
            filtered = [{name: item.get(name) for name in (
                "file_path", "existing_code", "symbol", "expected_side",
                "resolved_start_line", "resolved_end_line", "resolved_side",
            )} for item in evidence if isinstance(item, dict)]
            claim[field] = (filtered[0] if filtered else None) if (
                field == "primary_evidence") else filtered
        sample = result["sample"]
        blind_id = "blind-" + _identity([sample["head_sha"], key])[:24]
        snapshot = result.get("snapshot") or {}
        packets.append({"blind_id": blind_id, "snapshot": {
            "repository": sample["repository"],
            "declared_base_sha": sample["base_sha"],
            "base_sha": snapshot.get("verified_base") or sample["base_sha"],
            "head_sha": snapshot.get("verified_head") or sample["head_sha"],
            "diff_sha256": snapshot.get("diff_sha256"),
        }, "claim": claim, "quality_label": None})
        stages = [entry for entry in result.get("issue_audit", [])
                  if (entry.get("review_unit_id"), entry.get("issue_id")) == key]
        mapping.append({"blind_id": blind_id, "source_identity": list(key),
                        "source_sequence": event.get("sequence"),
                        "pipeline_stages": [{name: entry.get(name) for name in (
                            "stage", "status", "reason", "canonical_id",
                            "canonical_review_unit_id",
                        )} for entry in stages]})
    # 确定性哈希顺序，避免将原始 stage/终态顺序泄露给评审者。
    return sorted(packets, key=lambda item: item["blind_id"]), mapping


def quality_summary(packets: list[dict], annotations: list[dict]) -> dict[str, Any]:
    """独立评审标注必须完整、唯一且有理由；未标注保持 not_reviewed。

    duplicate 必须引用同包内已标 valid_defect 的对象；不允许 confirmed
    或参考匹配结果成为质量标签。证据字段供人工核验，不自动证明正确。
    """
    identities = {item["blind_id"] for item in packets}
    if len(identities) != len(packets):
        raise ValueError("duplicate packet identity")
    by_id = {}
    allowed = {"valid_defect", "false_positive", "duplicate", "cannot_determine"}
    for item in annotations:
        identity = item.get("blind_id")
        if identity not in identities or identity in by_id:
            raise ValueError("unknown or duplicate quality identity")
        if item.get("label") not in allowed or not str(item.get("reason") or "").strip():
            raise ValueError("quality annotation requires an independent label and reason")
        if item["label"] in {"valid_defect", "false_positive"} and not item.get("code_evidence"):
            raise ValueError("semantic quality requires independently inspected code evidence")
        by_id[identity] = item
    for identity, item in by_id.items():
        if item["label"] == "duplicate":
            target = by_id.get(item.get("duplicate_of"))
            if not target or target["blind_id"] == identity or target["label"] != "valid_defect":
                raise ValueError("duplicate must bind an independently valid defect")
    counts = Counter(item["label"] for item in by_id.values())
    return {"candidate_count": len(packets), "reviewed": len(by_id),
            "not_reviewed": len(identities - set(by_id)),
            **{label: counts[label] for label in sorted(allowed)},
            "precision": None, "recall": None,
            "reason": "candidate adjudication is not a reference recall evaluation"}
