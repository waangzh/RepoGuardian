"""最小证据能力实验与调查增量的离线契约；不调用 Provider。"""

import argparse
import json
from pathlib import Path

from app.services.fingerprints import stable_hash


VARIABLES = {"model", "reasoning", "dependency_docs", "task_expression", "context"}


def freeze_capability_plan(cases, baseline, variants, *, development_snapshots=()):
    ids, snapshots = set(), {}
    forbidden = {tuple(value) for value in development_snapshots}
    frozen = []
    for case in cases:
        identity, split = case["case_id"], case["split"]
        if not isinstance(identity, str) or not identity or identity in ids:
            raise ValueError("capability case identity must be unique")
        if split not in {"diagnostic", "holdout"}:
            raise ValueError("capability split invalid")
        ids.add(identity)
        snapshot = case["snapshot"]
        key = (snapshot["repository"], snapshot["base_sha"], snapshot["head_sha"])
        if key in snapshots and snapshots[key] != split or split == "holdout" and key in forbidden:
            raise ValueError("holdout overlaps development evidence")
        snapshots[key] = split
        evidence = case["evidence"]
        if not isinstance(evidence, list) or not evidence or any(
                not isinstance(item.get("content"), str) or not item["content"].strip() for item in evidence):
            raise ValueError("fixed necessary evidence required")
        # 评审答案不得混入可派发输入。case 外部维护独立评分材料。
        if set(case) != {"case_id", "split", "snapshot", "question", "evidence"}:
            raise ValueError("capability input contains unexpected fields")
        frozen.append({**case, "input_sha256": stable_hash(case)})
    if not frozen or set(baseline) != VARIABLES:
        raise ValueError("explicit baseline variables required")
    comparisons = []
    for name, settings in variants.items():
        changed = [key for key in VARIABLES if baseline[key] != settings.get(key)]
        if set(settings) != VARIABLES or len(changed) != 1:
            raise ValueError("each capability comparison must change exactly one variable")
        comparisons.append({"name": name, "variable": changed[0], "settings": settings})
    payload = {"version": "runtime-capability-plan-v1", "cases": frozen, "baseline": baseline,
        "comparisons": comparisons, "development_snapshots": sorted(forbidden),
        "provider_calls": False, "quality_status": "not_run", "default_switch_allowed": False}
    return {**payload, "plan_sha256": stable_hash(payload)}


def investigation_increment(baseline, investigated):
    """输入为独立裁决后的同样本汇总；保留 unknown，不能由管线 confirmed 代替。"""
    if baseline["sample_snapshot"] != investigated["sample_snapshot"]:
        raise ValueError("increment requires the same frozen snapshot")
    if baseline["configuration"] != investigated["configuration"]:
        raise ValueError("increment requires identical model/prompt/budget conditions")
    for result in (baseline, investigated):
        if result.get("adjudication_status") != "complete" or result.get("blind") is not True:
            raise ValueError("increment requires independent completed adjudication")
        if result.get("defect_identity_status") != "independently_matched":
            raise ValueError("increment requires independently matched defect identities")
        if not isinstance(result.get("valid_defect_ids"), list) or not isinstance(result.get("false_positive_ids"), list):
            raise ValueError("increment requires explicit semantic labels")
    before, after = set(baseline["valid_defect_ids"]), set(investigated["valid_defect_ids"])
    tokens_before, tokens_after = baseline.get("tokens"), investigated.get("tokens")
    known = (all(type(value) is int and value >= 0 for value in (tokens_before, tokens_after))
             and all(value.get("usage_integrity") == "complete" for value in (baseline, investigated)))
    return {"version": "investigation-increment-v1", "baseline_sha256": stable_hash(baseline),
        "investigated_sha256": stable_hash(investigated), "new_valid_defects": sorted(after - before),
        "lost_valid_defects": sorted(before - after),
        "false_positive_delta": len(set(investigated["false_positive_ids"])) - len(set(baseline["false_positive_ids"])),
        "additional_tokens": tokens_after - tokens_before if known else None,
        "usage_integrity": "complete" if known else "unknown",
        "causal_attribution": "paired_run_difference_only_requires_trace_adjudication",
        "default_switch_allowed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["freeze", "increment"])
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    value = json.loads(args.input.read_text(encoding="utf-8"))
    result = (freeze_capability_plan(**value) if args.action == "freeze"
              else investigation_increment(**value))
    with args.output.open("x", encoding="utf-8") as output:
        output.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
