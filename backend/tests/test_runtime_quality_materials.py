from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.evaluation.data import sha256
from app.evaluation.runtime_quality_gate import recompute_quality_gate, validate_material_quality_gate
from app.evaluation.runtime_experiments import screen_empty_candidate_regression
from test_runtime_experiments import frozen_plan
from test_task_risk_planner import relationship_fixture
from app.services.review.review_task_derivation import build_task_risk_catalog
from app.tools.repository_graph import build_repository_graph


def test_same_name_external_call_remains_search_hint_and_cannot_schedule():
    files = [{"path": "caller.py", "language": "python", "import_refs": [{"module": "external_sdk"}]},
             {"path": "unrelated.py", "language": "python"}]
    symbols = [{"file": "caller.py", "symbol": "run", "calls": ["external_sdk.parse"]},
               {"file": "unrelated.py", "symbol": "parse"}]
    edge = next(edge for edge in build_repository_graph(files, symbols)["edges"] if edge["type"] == "calls")
    assert edge["resolution_status"] == "unresolved" and edge["resolution_basis"] == "unique_name"
    goals, items, graph = relationship_fixture()
    graph["edges"][0].update(resolution_status="unresolved", resolution_basis="unique_name")
    risk = build_task_risk_catalog(goals, items, graph, planner_version="risk-planner-v4")[0]
    assert risk["not_scheduled_reason"] == "relationship_resolution_unverified" and not risk["evidence_requests"]
    assert build_task_risk_catalog(goals, items, graph, planner_version="risk-planner-v3")[0]["evidence_requests"]


def test_explicit_import_module_binding_can_schedule():
    files = [{"path": "caller.py", "language": "python", "import_refs": [{"module": "unrelated"}]},
             {"path": "unrelated.py", "language": "python"}]
    symbols = [{"file": "caller.py", "symbol": "run", "calls": ["unrelated.parse"]},
               {"file": "unrelated.py", "symbol": "parse"}]
    edge = next(edge for edge in build_repository_graph(files, symbols)["edges"] if edge["type"] == "calls")
    assert edge["resolution_status"] == "resolved"
    goals, items, graph = relationship_fixture()
    graph["edges"][0].update(resolution_status=edge["resolution_status"], resolution_basis=edge["resolution_basis"])
    assert build_task_risk_catalog(goals, items, graph, planner_version="risk-planner-v4")[0]["evidence_requests"]


@pytest.mark.parametrize("response", ['{"kind":"read"}', '{"kind":"submit"}', '{"kind":"submit","candidates":[]}'])
def test_incomplete_or_unaccepted_submit_is_not_semantic_loss(response):
    result = {"sample": {"instance_id": "x", "repository": "repo", "head_sha": "sha"},
              "task_runtime_report": {"findings": [], "scope_complete": True},
              "model_calls": [{"raw_response": response}]}
    packets = [{"blind_id": "old", "snapshot": {"repository": "repo", "head_sha": "sha"}}]
    adjudication = {"adjudication_status": "complete", "blind": True,
                    "accepted": [{"blind_id": "old", "label": "valid_defect"}]}
    assert screen_empty_candidate_regression(result, packets, adjudication)["status"] == "not_established"


def test_gate_recomputes_annotations_retention_and_hash_chain(tmp_path, monkeypatch):
    import app.evaluation.runtime_quality_gate as module
    plan = frozen_plan()
    sample = {"instance_id": "first"}
    old_result = {"sample": sample, "diff_input": {}, "old": True}
    result = {"sample": sample, "diff_input": {}}
    snapshot = {"repository": "repo", "head_sha": "a" * 40, "base_sha": "b" * 40}
    def packets(value):
        return [{"blind_id": "old" if value.get("old") else "new", "snapshot": snapshot}], []
    monkeypatch.setattr(module, "blind_review_packets", packets)
    monkeypatch.setattr(module, "reconcile_wire_usage", lambda value: {"actual_total_tokens": 50})
    first = {"admitted": True, "fixed_scope_coverage": {"not_worse_than_baseline": True}}
    monkeypatch.setattr(module, "first_sample_gate", lambda *args: first)
    def save(name, value):
        path = tmp_path / (name + ".json")
        content = json.dumps(value).encode()
        path.write_bytes(content)
        return {"path": str(path), "sha256": sha256(content)}
    baseline = save("baseline", old_result)
    plan["samples"] = {"first": {"sample": sample, "diff_input": {}, "tokens": 100,
        "baseline_result_path": baseline["path"], "baseline_result_sha256": baseline["sha256"]}}
    evidence = [{"path": "src/a.py", "side": "head", "snapshot_sha": snapshot["head_sha"],
                 "start_line": 1, "end_line": 1, "code": "return None"}]
    def annotations(identity):
        return {"reviewer_id": "independent", "blind": True, "annotations": [
            {"blind_id": identity, "label": "valid_defect", "reason": "源码支持", "code_evidence": evidence}]}
    retention = {"reviewer_id": "independent", "blind": True, "annotations": [
        {"blind_id": "old", "status": "retained", "retained_by": ["new"], "reason": "同一触发条件",
         "code_evidence": evidence}]}
    materials = {"result": save("result", result), "baseline_annotations": save("old", annotations("old")),
                 "annotations": save("new", annotations("new")), "retention": save("retention", retention),
                 "source_roots": save("roots", {})}
    def reader(*args):
        return "return None"
    gate = recompute_quality_gate(plan, "protocol_180", materials, source_reader=reader)
    assert gate["quality_passed"] and gate["metrics"]["lost_baseline_defects"] == 0
    monkeypatch.setattr(module, "make_source_reader", lambda *args: reader)
    materials["source_roots"] = save("roots", {"repositories": [], "workspace_root": str(tmp_path)})
    gate = recompute_quality_gate(plan, "protocol_180", materials, source_reader=reader)
    validate_material_quality_gate(plan, "protocol_180", first, gate)
    lost = deepcopy(retention)
    lost["annotations"][0].update(status="lost", retained_by=[])
    materials["retention"] = save("retention", lost)
    failed = recompute_quality_gate(plan, "protocol_180", materials, source_reader=reader)
    assert failed["metrics"]["lost_baseline_defects"] == 1 and not failed["expand_allowed"]
    materials["retention"] = save("retention", retention)
    bad = deepcopy(gate)
    bad["metrics"]["lost_baseline_defects"] = 1
    with pytest.raises(ValueError, match="differs from recomputed"):
        validate_material_quality_gate(plan, "protocol_180", first, bad)
    Path(materials["annotations"]["path"]).write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="material verification failed"):
        validate_material_quality_gate(plan, "protocol_180", first, gate)
