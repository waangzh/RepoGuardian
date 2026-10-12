"""从指定 r9 产物生成本地计量与盲审包；不运行审查图或模型。"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from app.evaluation.runtime_baseline import (
    blind_review_packets,
    quality_summary,
    reconcile_wire_usage,
)


def build_baseline(paths: list[Path]) -> tuple[dict, list[dict], list[dict]]:
    samples, packets, mapping = [], [], []
    seen = set()
    for path in paths:
        raw = path.read_bytes()
        result = json.loads(raw)
        identity = result["sample"]["instance_id"]
        if identity in seen:
            raise ValueError("duplicate baseline sample")
        seen.add(identity)
        usage = reconcile_wire_usage(result)
        blind, sources = blind_review_packets(result)
        packets.extend(blind)
        mapping.extend(sources)
        samples.append({
            "sample_id": identity, "artifact_sha256": hashlib.sha256(raw).hexdigest(),
            "snapshot": {key: (result.get("snapshot") or {}).get(key) for key in (
                "verified_head", "verified_base", "diff_policy", "diff_sha256",
            )},
            "pipeline_status": result.get("status"),
            "legacy_coverage": result.get("coverage"), "usage": usage,
            "quality": quality_summary(blind, []),
        })
    if not samples:
        raise ValueError("baseline requires explicit sample artifacts")
    report = {
        "version": "review-runtime-baseline-v1", "engine": "legacy_units",
        "evaluation_kind": "offline_artifact_audit",
        "quality_gate": "pending_independent_adjudication",
        "samples": samples,
        "wire_attempts": sum(item["usage"]["wire_attempts"] for item in samples),
        "actual_total_tokens": (sum(item["usage"]["actual_total_tokens"] for item in samples)
                                if all(item["usage"]["integrity"] == "complete"
                                       for item in samples) else None),
    }
    return report, sorted(packets, key=lambda item: item["blind_id"]), mapping


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path,
                        help="全新本地目录；包含审查原文，禁止直接提交")
    args = parser.parse_args()
    report, packets, mapping = build_baseline(args.artifacts)
    # 不覆盖旧结果；独立目录避免给评审者混入 pipeline map。
    args.output.mkdir(parents=True, exist_ok=False)
    for name, value in (
        ("accounting.json", report), ("blind/packets.json", packets),
        ("scorer/pipeline-map.json", mapping),
    ):
        path = args.output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"samples": len(report["samples"]),
                      "wire_attempts": report["wire_attempts"],
                      "actual_total_tokens": report["actual_total_tokens"],
                      "quality_gate": report["quality_gate"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
