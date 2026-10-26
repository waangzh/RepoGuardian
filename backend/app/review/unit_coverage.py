"""历史 Diff 清单和覆盖账本的只读核验。"""
from __future__ import annotations

from app.models.review import FileChangeEvidence
from app.evidence.file_change_evidence import validate_file_change_evidence
from app.core.fingerprints import stable_hash
NO_HUNK_CHANGE_REASON = "file_change_without_text_hunks"


def unsupported_hunk_ids(manifest, active=None):
    """A file inventory without a Diff body is not verifiable metadata evidence."""
    selected = {item.manifest_id for item in active.hunks} if active else None
    return [item.id for item in manifest.hunks if item.line_count == 0 and item.evidence_id is None
            and (selected is None or item.id in selected)]


def manifest_file_changes(manifest):
    result = {}
    for file in manifest.files:
        if file.get("file_change_evidence"):
            evidence = FileChangeEvidence.model_validate(file["file_change_evidence"])
            validate_file_change_evidence(evidence, manifest.snapshot["head_sha"], manifest.snapshot["base_sha"], file["file_path"])
            if evidence.id in result:
                raise ValueError("duplicate_file_change_evidence")
            result[evidence.id] = evidence
    return result


def _covers(ranges, start, end):
    if not ranges:
        return False
    cursor = start
    for left, right in sorted(ranges):
        if left > cursor:
            return False
        cursor = max(cursor, right)
        if cursor >= end:
            return True
    return False


def coverage_gaps(manifest, ledger, active=None, plan=None):
    if (manifest is None or ledger is None or manifest.input_hash != ledger.manifest_hash
            or manifest.review_unit_id != ledger.review_unit_id or manifest.snapshot != ledger.snapshot
            or manifest.input_hash != stable_hash(manifest.model_dump(mode="json", exclude={"input_hash"}))
            or {item.id for item in manifest.hunks} != set(ledger.diff_hunks)):
        return ["coverage_manifest_mismatch"]
    unsupported = set(unsupported_hunk_ids(manifest))
    try:
        changes = manifest_file_changes(manifest)
    except ValueError:
        return ["file_change_manifest_invalid"]
    if set(changes) != set(ledger.file_changes):
        return ["file_change_ledger_mismatch"]
    required_changes = set(active.file_change_evidence_ids) if active else set(changes)
    selected_paths = {item.file_path for item in manifest.hunks
        if active is None or item.id in {hunk.manifest_id for hunk in active.hunks}}
    if active and set(active.file_change_evidence_ids) != {identity for identity, evidence in changes.items()
            if evidence.file_path in selected_paths}:
        return ["active_file_change_inventory_mismatch"]
    metadata_gaps = [file["file_path"] + ":metadata_unverified" for file in manifest.files
        if file["file_path"] in selected_paths and file.get("metadata_required") and not file.get("file_change_evidence")]
    metadata_gaps += [identity for identity in required_changes if identity not in changes or not (
        ledger.file_changes[identity].metadata_verified and ledger.file_changes[identity].impact_status == "checked"
        and ledger.file_changes[identity].evidence_ids)]
    if active is not None:
        if active.manifest_hash != manifest.input_hash or not active.hunks:
            return ["active_manifest_mismatch"]
        return metadata_gaps + [item.manifest_id for item in active.hunks if item.manifest_id in unsupported or item.manifest_id not in ledger.diff_hunks
                or ledger.diff_hunks[item.manifest_id].status == "needs_followup"
                or not _covers(ledger.diff_hunks[item.manifest_id].reviewed_ranges, item.start_offset, item.end_offset)]
    for hunk in manifest.hunks:
        check = ledger.diff_hunks[hunk.id]
        if check.status == "reviewed" and (hunk.id in unsupported or not check.evidence_ids or
            any(not 0 <= left <= right <= hunk.line_count for left, right in check.reviewed_ranges)
            or not _covers(check.reviewed_ranges, 0, hunk.line_count)):
            return [hunk.id]
    if plan:
        for target in [*plan.review_objectives, *plan.coverage_targets]:
            if target not in ledger.targets or not ledger.targets[target].required:
                return [target]
        for hypothesis in plan.risk_hypotheses:
            if hypothesis.priority == "high" and (hypothesis.id not in ledger.hypotheses or not ledger.hypotheses[hypothesis.id].required):
                return [hypothesis.id]
    return [*metadata_gaps, *[key for key, check in ledger.diff_hunks.items() if check.status != "reviewed"],
            *[key for key, check in ledger.targets.items() if check.required and (check.status != "checked" or not check.evidence_ids)],
            *[key for key, check in ledger.hypotheses.items() if check.required and (check.status not in {"supported", "refuted"} or not check.evidence_ids)],
            *[key for key, check in ledger.questions.items() if check.status == "pending"],
            *[key for key, check in ledger.dependencies.items() if check.status != "verified"]]
