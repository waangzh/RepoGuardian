"""从固定材料重算质量门；哈希和源码验收不证明语义标签正确。"""

from pathlib import Path
import json

from app.evaluation.data import sha256
from app.evaluation.runtime_baseline import blind_review_packets, reconcile_wire_usage
from app.evaluation.runtime_adjudication import adjudicate_annotations
from app.evaluation.runtime_adjudication_cli import make_source_reader
from app.services.fingerprints import stable_hash


def recompute_quality_gate(plan, group, materials, *, source_reader=None):
    required = {"result", "baseline_annotations", "annotations", "retention", "source_roots"}
    if not isinstance(materials, dict) or set(materials) != required:
        raise ValueError("quality gate requires fixed adjudication materials")
    loaded = {}
    for name, reference in materials.items():
        content = Path(reference["path"]).read_bytes()
        if sha256(content) != reference["sha256"]:
            raise ValueError("quality material hash mismatch: " + name)
        loaded[name] = json.loads(content)
    baseline = plan["samples"][plan["first_sample_id"]]
    content = Path(baseline["baseline_result_path"]).read_bytes()
    if sha256(content) != baseline["baseline_result_sha256"]:
        raise ValueError("quality baseline hash mismatch")
    old_result = json.loads(content)
    result = loaded["result"]
    if result["sample"] != baseline["sample"] or result["diff_input"] != baseline["diff_input"]:
        raise ValueError("quality result snapshot mismatch")
    if old_result["sample"] != result["sample"] or old_result["diff_input"] != result["diff_input"]:
        raise ValueError("quality baseline snapshot mismatch")
    roots = loaded["source_roots"]
    reader = source_reader or make_source_reader(roots["repositories"], Path(roots["workspace_root"]))
    old_packets, _ = blind_review_packets(old_result)
    packets, _ = blind_review_packets(result)
    reports = []
    for packet_set, name in ((old_packets, "baseline_annotations"), (packets, "annotations")):
        annotations = loaded[name]
        reports.append(adjudicate_annotations(packet_set, annotations["annotations"], source_reader=reader,
            reviewer_id=annotations["reviewer_id"], blind=annotations["blind"]))
    old, new = reports
    valid_old = {item["blind_id"] for item in old["accepted"] if item["label"] == "valid_defect"}
    valid_new = {item["blind_id"] for item in new["accepted"] if item["label"] == "valid_defect"}
    retention = loaded["retention"]
    bindings = retention["annotations"]
    if {item.get("blind_id") for item in bindings} != valid_old or len(bindings) != len(valid_old):
        raise ValueError("quality retention requires every historical valid defect")
    # 用原有标注验收核对对应历史缺陷的源码，标签仅表达独立评审提供的保留判断。
    retention_packets = [packet for packet in old_packets if packet["blind_id"] in valid_old]
    normalized = []
    retained = set()
    for item in bindings:
        targets = item.get("retained_by")
        if not isinstance(targets, list) or len(set(targets)) != len(targets) or not set(targets) <= valid_new:
            raise ValueError("quality retention binding invalid")
        if ((not targets and item.get("status") != "lost")
                or (targets and item.get("status") != "retained") or not item.get("code_evidence")):
            raise ValueError("quality retention status invalid")
        normalized.append({key: item[key] for key in ("blind_id", "reason", "code_evidence")}
                          | {"label": "valid_defect"})
        if targets:
            retained.add(item["blind_id"])
    comparison = adjudicate_annotations(retention_packets, normalized, source_reader=reader,
        reviewer_id=retention["reviewer_id"], blind=retention["blind"])
    from app.evaluation.runtime_experiments import first_sample_gate
    first = first_sample_gate(result, plan, group)
    accounting = reconcile_wire_usage(result)
    tokens = accounting["actual_total_tokens"]
    complete = all(report["adjudication_status"] == "complete" for report in (*reports, comparison))
    metrics = {"token_reduction": 1 - tokens / baseline["tokens"] if tokens is not None and baseline["tokens"] else None,
        "lost_baseline_defects": len(valid_old - retained),
        "new_false_positives": new["quality"]["false_positive"],
        "baseline_false_positives": old["quality"]["false_positive"],
        "new_cannot_determine": new["quality"]["cannot_determine"]}
    passed = (complete and first["admitted"] and first["fixed_scope_coverage"]["not_worse_than_baseline"]
        and metrics["token_reduction"] is not None and metrics["token_reduction"] >= plan["screening"]["token_reduction"]
        and metrics["lost_baseline_defects"] == 0
        and metrics["new_false_positives"] <= metrics["baseline_false_positives"]
        and metrics["new_cannot_determine"] == 0)
    payload = {"version": "runtime-quality-gate-v2", "calculation_version": "material-recompute-v1",
        "plan_sha256": plan["plan_sha256"], "group": group, "sample_id": plan["first_sample_id"],
        "result_sha256": stable_hash(result), "adjudication_status": "complete" if complete else "pending",
        "quality_passed": passed, "expand_allowed": passed, "default_switch_allowed": False,
        "metrics": metrics, "materials": materials, "first_gate": first}
    return {**payload, "gate_sha256": stable_hash(payload)}


def validate_material_quality_gate(plan, group, first_gate, gate):
    if not gate or gate.get("version") != "runtime-quality-gate-v2":
        raise ValueError("remaining samples require an independent quality gate with materials")
    try:
        computed = recompute_quality_gate(plan, group, gate.get("materials"))
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("independent quality gate material verification failed") from exc
    if gate != computed or first_gate != computed["first_gate"]:
        raise ValueError("independent quality gate differs from recomputed materials")


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--group", required=True)
    parser.add_argument("--materials", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    gate = recompute_quality_gate(json.loads(args.plan.read_text(encoding="utf-8")), args.group,
                                  json.loads(args.materials.read_text(encoding="utf-8")))
    with args.output.open("x", encoding="utf-8") as output:
        output.write(json.dumps(gate, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
