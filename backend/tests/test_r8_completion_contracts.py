from copy import deepcopy

import pytest

from app.models.review import ReviewUnitResult, UnitReviewRecord
from app.review.unit_completion import is_review_unit_complete
from app.services.cross_unit_coordination import CrossUnitCoordinationService
from app.services.followup_contracts import reconcile_contracts
from app.services.unit_coverage import build_diff_manifest, new_coverage_ledger, active_evidence_set, update_coverage
from app.services.unit_review_summary import build_record_input, validate_record
from test_cross_unit_coordination import state


def bound_fixture():
    value = state()
    from app.models.review import ChangedFile, ReviewUnit
    units = [ReviewUnit.model_validate(raw) for raw in value["review_units"]]
    files = [ChangedFile.model_validate(raw) for raw in value["changed_files"]]
    parent = ReviewUnitResult.model_validate(value["review_unit_results"][0])
    manifest = build_diff_manifest(units[0], [files[0]], "h", "b")
    active = active_evidence_set(manifest, [files[0]], units[0].id)
    request = build_record_input(files, [], None, "h", "b")
    request["diff_manifest"] = manifest.model_dump(mode="json")
    dep = {"file_path": "callee.py", "symbol": "load", "assumption": "返回值契约", "status": "unresolved", "evidence_ids": []}
    summary = validate_record(UnitReviewRecord(change_summary="检查调用方", target_checks=[{
        "target": target, "status": "checked", "reason": "检查", "evidence_ids": [request["evidence"][0]["id"]]}
        for target in request["targets"]], contract_dependencies=[dep]), request, {"caller.py", "callee.py"})
    from app.models.review import UnitInputCoverage
    parent = parent.model_copy(update={"review_summary": summary, "diff_manifest": manifest,
        "active_evidence_set": active, "coverage_ledger": update_coverage(new_coverage_ledger(manifest), manifest, active, summary),
        "input_coverage": UnitInputCoverage(evidence_coverage="complete", target_coverage="complete", contract_coverage="partial")})
    value["review_unit_results"][0] = parent.model_dump(mode="json")
    origin = CrossUnitCoordinationService.catalog(value)["origin_obligations"][0]
    follow_input = build_record_input(files, [], None, "h", "b")
    follow_input["origin_obligations"] = [origin]
    evidence = follow_input["evidence"][1]["id"]
    record = UnitReviewRecord(change_summary="补查返回值", target_checks=[{
        "target": target, "status": "checked", "reason": "检查", "evidence_ids": [evidence]}
        for target in follow_input["targets"]], obligation_updates=[{
            "obligation_id": origin["id"], "status": "verified", "evidence_ids": [evidence], "reason": "核验定义"}])
    follow_summary = validate_record(record, follow_input, {"caller.py", "callee.py"})
    followup = ReviewUnitResult(review_unit_id="followup-one", status="completed", terminal_reason="no_issue", review_summary=follow_summary)
    request = {"id": "f1", "origin_obligation_ids": [origin["id"]], "unit_ids": ["u0", "u1"], "primary_files": ["caller.py", "callee.py"]}
    value["coordination_plan"] = {"followups": [request]}
    saved = {"request_id": "f1", "validation_status": "completed", "outcome": "refuted", "unit_result": followup.model_dump(mode="json")}
    return value, [saved], origin, follow_input, record


def test_explicit_verified_contract_closure_preserves_parent_identity_ranges_and_history():
    value, saved, origin, _, _ = bound_fixture()
    before = deepcopy(value)
    result = reconcile_contracts(value, saved, CrossUnitCoordinationService.catalog)
    parent = ReviewUnitResult.model_validate(result[0])
    assert value == before and parent.review_unit_id == "u0"
    assert parent.coverage_ledger.dependencies[origin["check_id"]].status == "verified"
    assert parent.diff_manifest.model_dump() == before["review_unit_results"][0]["diff_manifest"]
    assert parent.coverage_ledger.diff_hunks == ReviewUnitResult.model_validate(before["review_unit_results"][0]).coverage_ledger.diff_hunks
    assert parent.review_summary.record_history[-2].contract_dependencies[0].status == "unresolved"
    assert is_review_unit_complete(parent)
    assert reconcile_contracts({**value, "review_unit_results": result}, saved, CrossUnitCoordinationService.catalog) is None


@pytest.mark.parametrize("change", ["head", "base", "invalid_parent", "invalid_followup", "missing_evidence", "unbound", "wrong_unit", "unknown_origin", "pending"])
def test_untrusted_or_incomplete_followup_cannot_close_parent(change):
    value, saved, _, _, _ = bound_fixture()
    if change in {"head", "base"}:
        saved[0]["unit_result"]["review_summary"]["last_valid_snapshot"][change + "_sha"] = "different"
    elif change == "invalid_parent":
        value["review_unit_results"][0]["review_summary"]["latest_attempt_status"] = "invalid"
    elif change == "invalid_followup":
        saved[0]["unit_result"]["review_summary"]["latest_attempt_status"] = "invalid"
    elif change == "missing_evidence":
        saved[0]["unit_result"]["review_summary"]["record"]["obligation_updates"][0]["evidence_ids"] = ["stale"]
    elif change == "unbound":
        value["coordination_plan"]["followups"][0]["origin_obligation_ids"] = []
    elif change == "wrong_unit":
        value["coordination_plan"]["followups"][0]["unit_ids"] = ["u1"]
    elif change == "unknown_origin":
        saved[0]["unit_result"]["review_summary"]["record"]["obligation_updates"][0]["obligation_id"] = "unknown"
    else:
        saved[0]["validation_status"] = "pending"
    assert reconcile_contracts(value, saved, CrossUnitCoordinationService.catalog) is None


@pytest.mark.parametrize("change", ["unknown", "wrong_status", "wrong_file"])
def test_obligation_record_rejects_fabricated_identity_status_or_evidence(change):
    _, _, _, request, record = bound_fixture()
    update = record.obligation_updates[0]
    if change == "unknown":
        update.obligation_id = "unknown"
    elif change == "wrong_status":
        update.status = "resolved"
    else:
        update.evidence_ids = [request["evidence"][0]["id"]]
    assert validate_record(record, request, {"caller.py", "callee.py"}).latest_attempt_status == "invalid"
