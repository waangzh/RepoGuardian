import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.evaluation.runtime_baseline import (
    blind_review_packets,
    quality_summary,
    reconcile_wire_usage,
)


def _result():
    usage = {
        "id": "usage-1", "usage_available": True,
        "actual_input_tokens": 8, "actual_output_tokens": 2,
        "actual_total_tokens": 10,
        "response_metadata": {"request_admission": {"request_hash": "hash-1"}},
    }
    return {
        "sample": {"repository": "org/repo", "base_sha": "base", "head_sha": "head"},
        "model_requests": [
            {"request_id": "request-1", "attempt": 1, "status": "started", "operation": "review_step"},
            {"request_id": "request-1", "attempt": 1, "status": "completed", "usage": usage},
        ],
        "model_calls": [{"usage": usage}],
        "run_budget": {"calls_consumed": 1, "settled_actual": 10,
                       "in_flight_reserved": 0, "unknown_reserved": 0,
                       "attempts": {"attempt-1": {
                           "attempt_id": "attempt-1", "request_id": "request-1",
                           "request_hash": "hash-1", "actual_tokens": 10,
                           "settled": True, "sent": True,
                       }}},
        "issue_audit": [],
    }


def test_reconcile_wire_usage_matches_budget_without_guessing():
    report = reconcile_wire_usage(_result())
    assert report["integrity"] == "complete"
    assert report["actual_total_tokens"] == 10
    assert report["attempts"][0]["attempt_id"] == "attempt-1"


def _task_result():
    return {
        "engine": "task_review_v1",
        "sample": {"repository": "org/repo", "base_sha": "b" * 40, "head_sha": "a" * 40},
        "snapshot": {"verified_base": "b" * 40, "verified_head": "a" * 40,
                     "diff_sha256": "d" * 64},
        "model_requests": [{"status": "started", "operation": "task_review_v1",
            "request_id": "r1", "attempt": 1, "prompt": json.dumps({
            "task": {"kind": "inspect"},
            "snapshot": {"base_sha": "b" * 40, "head_sha": "a" * 40},
            "goals": [{"goal_id": "g1"}],
            "active_evidence": [{"id": "e1", "file_path": "src/a.py", "content": "return 0",
                                  "start_line": 1, "end_line": 1, "side": "head"}],
        })}, {"status": "completed", "request_id": "r1", "attempt": 1, "usage": {"id": "u1"}}],
        "model_calls": [{"usage": {"id": "u1"}, "raw_response": json.dumps({"kind": "submit", "candidates": [{
            "claim": "候选", "primary_anchor": "g1", "evidence_ids": ["e1"],
            "affected_behavior": "行为改变", "failure_scenario": "输入触发", "recommendation": "修复"}]} )}],
    }


def test_task_runtime_candidate_packets_are_extracted_without_pipeline_terminal_state():
    result = _task_result()
    packets, mapping = blind_review_packets(result)
    assert len(packets) == len(mapping) == 1
    assert packets[0]["quality_label"] is None
    assert packets[0]["claim"]["primary_evidence"]["file_path"] == "src/a.py"


def test_task_packet_binds_response_to_usage_identity_not_call_order():
    result = _task_result()
    other = deepcopy(result["model_requests"][0])
    other["request_id"] = "r2"
    context = json.loads(other["prompt"])
    context["active_evidence"][0]["content"] = "return 1"
    other["prompt"] = json.dumps(context)
    result["model_requests"] += [other, {"status": "completed", "request_id": "r2", "attempt": 1,
                                         "usage": {"id": "u2"}}]
    result["model_calls"].insert(0, {"usage": {"id": "u2"}, "raw_response": '{"kind":"submit","candidates":[]}'})
    packets, mapping = blind_review_packets(result)
    assert packets[0]["claim"]["primary_evidence"]["existing_code"] == "return 0"
    assert mapping[0]["request_id"] == "r1"


@pytest.mark.parametrize("mutation,error", [
    ("missing_array", "array_missing"), ("invalid_json", "observation_invalid"),
    ("missing_evidence", "evidence_missing"), ("wrong_usage", "response_identity_invalid")])
def test_task_packet_rejects_incomplete_observations(mutation, error):
    result = _task_result()
    call = result["model_calls"][0]
    response = json.loads(call["raw_response"])
    if mutation == "missing_array":
        del response["candidates"]
    elif mutation == "missing_evidence":
        response["candidates"][0]["evidence_ids"] = ["archived"]
    elif mutation == "wrong_usage":
        call["usage"]["id"] = "other"
    call["raw_response"] = "invalid" if mutation == "invalid_json" else json.dumps(response)
    with pytest.raises(ValueError, match=error):
        blind_review_packets(result)


def test_task_packet_deduplicates_claim_but_preserves_all_response_sources():
    result = _task_result()
    result["model_requests"] += [{**result["model_requests"][0], "request_id": "r2"},
        {"status": "completed", "request_id": "r2", "attempt": 1, "usage": {"id": "u2"}}]
    result["model_calls"].append({**result["model_calls"][0], "usage": {"id": "u2"}})
    packets, mapping = blind_review_packets(result)
    assert len(packets) == 1 and len(mapping) == 2
    assert {item["request_id"] for item in mapping} == {"r1", "r2"}


def test_task_packet_retry_uses_final_attempt_context():
    result = _task_result()
    result["model_requests"][0]["attempt"] = 2
    result["model_requests"][1]["attempt"] = 2
    failed = {**result["model_requests"][0], "attempt": 1}
    result["model_requests"].insert(0, failed)
    result["model_requests"].insert(1, {"status": "failed", "request_id": "r1", "attempt": 1})
    _, mapping = blind_review_packets(result)
    assert mapping[0]["wire_attempt"] == 2


def test_task_packet_rejects_stale_snapshot():
    result = _task_result()
    context = json.loads(result["model_requests"][0]["prompt"])
    context["snapshot"]["head_sha"] = "stale"
    result["model_requests"][0]["prompt"] = json.dumps(context)
    with pytest.raises(ValueError, match="snapshot_mismatch"):
        blind_review_packets(result)


def test_task_runtime_candidate_packet_rejects_missing_response_observation():
    result = {"engine": "task_review_v1", "sample": {"repository": "org/repo",
        "base_sha": "b" * 40, "head_sha": "a" * 40}, "model_requests": [], "model_calls": []}
    with pytest.raises(ValueError, match="observations_missing"):
        blind_review_packets(result)


def test_unknown_usage_is_not_zero_and_invalidates_complete_total():
    result = _result()
    result["model_requests"][1]["usage"] = {"id": "usage-1", "usage_available": False}
    result["model_calls"][0]["usage"] = None
    report = reconcile_wire_usage(result)
    assert report["integrity"] == "unknown"
    assert report["actual_total_tokens"] is None
    assert report["unknown_usage_attempts"] == 1


def test_pending_reservations_do_not_invent_an_extra_unknown_attempt():
    result = _result()
    result["run_budget"]["unknown_reserved"] = 50
    report = reconcile_wire_usage(result)
    assert report["integrity"] == "unknown"
    assert report["actual_total_tokens"] is None
    assert report["unknown_usage_attempts"] == 0
    assert report["unreconciled_reservations"] is True


def test_pre_send_rejection_is_separate_from_wire_usage():
    result = _result()
    result["model_requests"].append({
        "request_id": "rejected-1", "attempt": 1, "status": "admission_rejected",
    })
    report = reconcile_wire_usage(result)
    assert report["integrity"] == "complete"
    assert report["wire_attempts"] == 1 and report["admission_rejections"] == 1
    assert report["actual_total_tokens"] == 10
    result["model_requests"][-1]["request_id"] = "request-1"
    report = reconcile_wire_usage(result)
    assert report["integrity"] == "inconsistent"
    assert "admission_rejection_with_dispatch" in report["errors"]


@pytest.mark.parametrize("mutation,error", [
    ("duplicate_start", "duplicate_wire_start"),
    ("duplicate_outcome", "duplicate_wire_outcome"),
    ("wrong_ledger_tokens", "ledger_usage_mismatch"),
    ("wrong_total", "ledger_total_mismatch"),
    ("missing_high_level", "high_level_usage_mismatch"),
    ("bad_components", "usage_component_mismatch"),
    ("unsent", "wire_not_marked_sent"),
    ("ambiguous_ledger", "ledger_attempt_not_unique"),
])
def test_corrupt_accounting_cannot_pass(mutation, error):
    result = _result()
    ledger = result["run_budget"]["attempts"]["attempt-1"]
    if mutation == "duplicate_start":
        result["model_requests"].append(deepcopy(result["model_requests"][0]))
    elif mutation == "duplicate_outcome":
        result["model_requests"].append(deepcopy(result["model_requests"][1]))
    elif mutation == "wrong_ledger_tokens":
        ledger["actual_tokens"] = 9
    elif mutation == "wrong_total":
        result["run_budget"]["settled_actual"] = 9
    elif mutation == "missing_high_level":
        result["model_calls"] = []
    elif mutation == "bad_components":
        result["model_requests"][1]["usage"]["actual_input_tokens"] = 7
    elif mutation == "unsent":
        ledger["sent"] = False
    else:
        result["run_budget"]["attempts"]["attempt-2"] = {
            **ledger, "attempt_id": "attempt-2",
        }
    report = reconcile_wire_usage(result)
    assert report["integrity"] == "inconsistent" and error in report["errors"]
    assert report["actual_total_tokens"] is None


def _observations():
    path = Path(__file__).parent / "fixtures" / "review_runtime_r9" / "observations.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_frozen_r9_wire_attempts_recalculate_all_five_samples():
    fixture = _observations()
    reports = []
    for sample in fixture["samples"]:
        report = reconcile_wire_usage(sample["accounting_input"])
        assert report["integrity"] == "complete", report["errors"]
        assert report["wire_attempts"] == sample["expected"]["calls"]
        assert report["actual_total_tokens"] == sample["expected"]["tokens"]
        assert len(sample["source"]["sha256"]) == 64
        reports.append(report)
    assert len(reports) == 5
    assert sum(item["wire_attempts"] for item in reports) == 153
    assert sum(item["admission_rejections"] for item in reports) == 10
    assert sum(item["actual_total_tokens"] for item in reports) == 2_155_240


def test_checkmate_failure_reconstruction_uses_current_validator():
    from app.models.review import UnitReviewRecord
    from app.services.unit_review_summary import validate_record

    sample = next(item for item in _observations()["samples"]
                  if "Checkmate" in item["sample"]["instance_id"])
    assert len(sample["observed"]["followups"]) == 2
    for followup in sample["observed"]["followups"]:
        evidence = followup["evidence"]
        ids = [evidence[0]["id"]]
        # 原产物没有保存被拒响应；只重建造成相同校验失败的最小记录。
        record = UnitReviewRecord(change_summary="重建最小不可见旧问题失败", target_checks=[{
            "target": "reconstructed-check", "status": "checked",
            "evidence_ids": ids, "reason": "独立合法检查结果",
        }], question_updates=[{
            "question_id": "reconstructed-invisible-question", "status": "resolved",
            "evidence_ids": ids, "reason": "旧问题不在当前目录",
        }])
        request = {"targets": ["reconstructed-check"], "hypotheses": [],
                   "evidence": evidence, "snapshot": followup["input_snapshot"],
                   "input_protocol": "canonical-evidence-v3", "working_memory": {}}
        summary = validate_record(record, request, {item["file_path"] for item in evidence})
        assert summary.latest_attempt_status == "invalid"
        assert summary.latest_attempt_reason == followup["latest_attempt_reason"]
        assert summary.record is None  # legacy 整份拒绝也会丢掉合法兄弟检查。


def test_r9_observations_keep_scope_gaps_and_local_closure_separate():
    by_repo = {item["sample"]["repository"]: item for item in _observations()["samples"]}
    gemini = by_repo["google-gemini/gemini-cli"]
    assert sum(len(unit["batches"]) for unit in gemini["observed"]["units"]) == 8
    assert any("run_budget_admission_rejected" in text
               for text in gemini["observed"]["warnings"])
    assert gemini["expected"]["tokens"] == 996_050
    ollama = by_repo["ollama/ollama"]["observed"]
    assert all(unit["summary_status"] == "unknown" for unit in ollama["units"])
    assert any(batch["terminal_reason"] == "no_new_context"
               for unit in ollama["units"] for batch in unit["batches"])
    comfy = by_repo["comfyanonymous/ComfyUI"]["observed"]
    assert comfy["units"][0]["latest_attempt_reason"] == "validated_followup_contract_closure"
    followup = comfy["followups"][0]
    assert followup["outcome"] == "unresolved"
    assert followup["obligation_updates"][0]["status"] == "verified"
    assert followup["obligation_updates"][0]["obligation_id"] in followup["origin_obligation_ids"]


def test_blind_packet_hides_pipeline_status_and_quality_requires_code_evidence():
    result = _result()
    result["snapshot"] = {"verified_base": "merge-base", "verified_head": "head"}
    result["issue_audit"] = [{
        "sequence": 1, "stage": "generated", "review_unit_id": "unit-1",
        "issue_id": "issue-1", "status": "confirmed",
        "reason": None, "issue": {
            "title": "问题", "affected_behavior": "行为", "failure_scenario": "场景",
            "recommendation": "建议", "assumptions": [],
            "primary_evidence": {"file_path": "a.py", "existing_code": "x", "resolved_start_line": 1,
                                  "resolved_end_line": 1, "resolved_side": "head"},
            "supporting_evidence": [],
        },
    }]
    packets, mapping = blind_review_packets(result)
    assert len(packets) == 1 and packets[0]["quality_label"] is None
    assert packets[0]["snapshot"]["base_sha"] == "merge-base"
    assert packets[0]["snapshot"]["declared_base_sha"] == "base"
    assert "status" not in packets[0]["claim"]
    assert mapping[0]["pipeline_stages"][0]["status"] == "confirmed"
    summary = quality_summary(packets, [])
    assert summary["not_reviewed"] == 1 and summary["precision"] is None
    try:
        quality_summary(packets, [{"blind_id": packets[0]["blind_id"],
                                  "label": "valid_defect", "reason": "代码证据不足"}])
    except ValueError as exc:
        assert str(exc) == "semantic quality requires independently inspected code evidence"
    else:
        raise AssertionError("quality evidence must be independently supplied")
