"""服务端绑定的契约补查；只推进原问题/依赖，不授予文本检查覆盖。"""

from app.models.review import ReviewUnitResult
from app.services.fingerprints import stable_hash
from app.services.review_input_context import question_identity
from app.services.unit_coverage import classified_coverage_gaps
from app.review.diff_base import review_diff_base


def origin_obligations(state, available_evidence):
    obligations = []
    for raw in state.get("review_unit_results") or []:
        result = ReviewUnitResult.model_validate(raw)
        summary = result.review_summary
        if (summary.status != "reported" or summary.latest_attempt_status != "reported" or not summary.record
                or not result.diff_manifest or not result.coverage_ledger
                or summary.last_valid_snapshot.get("head_sha") != state.get("head_sha")
                or summary.last_valid_snapshot.get("base_sha", "") != review_diff_base(state)):
            continue
        record = summary.record
        checks = [("dependency", stable_hash([check.file_path, check.symbol, check.assumption]), check.model_dump(mode="json"))
                  for check in record.contract_dependencies if check.status != "verified"]
        checks += [("question", question_identity(check.model_dump(mode="json")), check.model_dump(mode="json"))
                   for check in record.unresolved_questions]
        for kind, check_id, check in checks[:20]:
            if set(check.get("evidence_ids", [])) - available_evidence:
                continue
            binding = {"unit_id": result.review_unit_id, "kind": kind, "check_id": check_id, "check": check,
                "input_fingerprint": result.input_fingerprint, "snapshot": summary.last_valid_snapshot,
                "manifest_hash": result.diff_manifest.input_hash if result.diff_manifest else None,
                "record_hash": stable_hash(record.model_dump(mode="json"))}
            obligations.append({"id": "obligation-" + stable_hash(binding)[:40], **binding})
    return obligations


def validate_obligation_updates(record, record_input, by_id):
    origins = {item["id"]: item for item in record_input.get("origin_obligations", [])}
    for update in record.obligation_updates:
        origin = origins.get(update.obligation_id)
        if not origin:
            raise ValueError("unknown origin obligation")
        expected = "verified" if origin["kind"] == "dependency" else "resolved"
        check = origin["check"]
        paths = {check["file_path"]} if origin["kind"] == "dependency" else set(check["affected_files"])
        cited = {by_id[identity].file_path for identity in update.evidence_ids}
        if update.status != expected or not paths or not paths <= cited:
            raise ValueError("obligation closure requires evidence for every bound file")


def reconcile_contracts(state, followups, catalog_builder):
    """生成新的父结果投影；保留原记录历史、身份和覆盖范围。"""
    if not followups:
        return None
    original = {raw["review_unit_id"]: ReviewUnitResult.model_validate(raw)
                for raw in state.get("review_unit_results") or []}
    payload = catalog_builder(state)
    origins = {item["id"]: item for item in payload.get("origin_obligations", [])}
    projected = {}
    requests = {item["id"]: item for item in (state.get("coordination_plan") or {}).get("followups", [])}
    # 调用方把当前已验证计划传入；旧无绑定结果不会获得回填权限。
    for saved in followups:
        request = requests.get(saved["request_id"], {})
        allowed = set(request.get("origin_obligation_ids", []))
        followup = ReviewUnitResult.model_validate(saved["unit_result"])
        summary = followup.review_summary
        if (saved.get("validation_status") != "completed" or followup.status != "completed"
                or summary.status != "reported" or summary.latest_attempt_status != "reported" or not summary.record
                or not summary.record.target_checks or any(check.status != "checked" for check in summary.record.target_checks)
                or summary.record.unresolved_questions or any(check.status != "verified" for check in summary.record.contract_dependencies)
                or any(issue.status in {"candidate", "evidence_resolved", "unresolved", "needs_human"} for issue in followup.issues)):
            continue
        units = [{"id": followup.review_unit_id, "primary_files": request.get("primary_files", []),
                  "related_files": sorted({file for key in request.get("unit_ids", []) for file in next(
                      (unit.get("primary_files", []) + unit.get("related_files", []) for unit in state.get("review_units", [])
                       if unit["id"] == key), [])}), "estimated_tokens": 0, "complexity": "small",
                  "fingerprint": followup.input_fingerprint or "", "grouping_reason": "contract_closure"}]
        trusted = {item["id"]: item for item in catalog_builder({**state, "review_units": units,
            "review_unit_results": [followup.model_dump(mode="json")]}).get("evidence", [])}
        for update in summary.record.obligation_updates:
            origin = origins.get(update.obligation_id)
            if not origin or update.obligation_id not in allowed or origin["unit_id"] not in request.get("unit_ids", []):
                continue
            parent = original[origin["unit_id"]]
            if any(summary.last_valid_snapshot.get(key, "") != origin["snapshot"].get(key, "")
                   for key in ("head_sha", "base_sha", "input_version", "pr_intent_hash", "provider_input_protocol")):
                continue
            if any(identity not in trusted for identity in update.evidence_ids):
                continue
            try:
                validate_obligation_updates(summary.record.model_copy(update={"obligation_updates": [update]}),
                    {"origin_obligations": [origin]}, {item.id: item for item in summary.evidence})
            except (KeyError, ValueError):
                continue
            result = projected.setdefault(parent.review_unit_id, parent.model_copy(deep=True))
            prior = result.review_summary.record
            if origin["kind"] == "dependency":
                for check in prior.contract_dependencies:
                    if stable_hash([check.file_path, check.symbol, check.assumption]) == origin["check_id"]:
                        check.status, check.evidence_ids = "verified", update.evidence_ids
                if result.coverage_ledger and origin["check_id"] in result.coverage_ledger.dependencies:
                    entry = result.coverage_ledger.dependencies[origin["check_id"]]
                    entry.status, entry.evidence_ids = "verified", update.evidence_ids
            else:
                prior.unresolved_questions = [check for check in prior.unresolved_questions
                    if question_identity(check.model_dump(mode="json")) != origin["check_id"]]
                if result.coverage_ledger and origin["check_id"] in result.coverage_ledger.questions:
                    result.coverage_ledger.questions[origin["check_id"]].status = "resolved"
            refs = {item.id: item for item in result.review_summary.evidence}
            refs.update({item.id: item for item in summary.evidence if item.id in update.evidence_ids})
            result.review_summary.evidence = list(refs.values())
            result.review_summary.last_valid_record = prior.model_copy(deep=True)
            result.review_summary.record_history = [*parent.review_summary.record_history, parent.review_summary.record, prior.model_copy(deep=True)][-3:]
            result.review_summary.latest_attempt_reason = "validated_followup_contract_closure"
            if result.coverage_ledger:
                for identity in update.evidence_ids:
                    result.coverage_ledger.supporting_evidence[identity] = refs[identity]
                gaps = classified_coverage_gaps(result.diff_manifest, result.coverage_ledger, plan=result.plan)
                if result.input_coverage:
                    result.input_coverage.contract_coverage = "partial" if gaps["questions"] or gaps["dependencies"] else "complete"
                    result.input_coverage.unresolved_questions = gaps["questions"]
                    result.input_coverage.unresolved_dependencies = gaps["dependencies"]
    return [projected.get(key, result).model_dump(mode="json") for key, result in original.items()] if projected else None
