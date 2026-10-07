"""完整 Diff 清单与有限活动集；覆盖状态只能由当前核验记录推导。"""
from __future__ import annotations

import json

from app.models.review import (DiffManifestHunk, UnitActiveEvidenceSet, UnitActiveHunk,
    UnitCoverageCheck, UnitCoverageLedger, UnitDiffManifest, UnitHunkCoverage, UnitQuestionCoverage)
from app.services.fingerprints import stable_hash
from app.services.review_planner import DeterministicReviewPlanner
from app.services.unit_review_summary import build_record_input
from app.services.review_input_context import question_identity

MAX_ACTIVE_HUNKS = 4
MAX_ACTIVE_DIFF_CHARS = 32_000


def build_diff_manifest(unit, files, head_sha, base_sha):
    record = build_record_input(files, [], None, head_sha, base_sha)
    references = iter(record["evidence"])
    hunks, metadata = [], []
    for file in files:
        ids = []
        for index, hunk in enumerate(file.hunks):
            identity = DeterministicReviewPlanner.hunk_id(file.file_path, index, hunk.model_dump(mode="json"))
            reference = next(references)
            key = "hunk-" + stable_hash([file.file_path, identity])[:24]
            if key in ids or any(item.id == key for item in hunks):
                raise ValueError("duplicate_manifest_hunk_identity")
            ids.append(key)
            hunks.append(DiffManifestHunk(id=key, file_path=file.file_path, hunk_id=identity,
                **{name: getattr(hunk, name) for name in ("old_start", "old_length", "new_start", "new_length")},
                line_count=len(hunk.lines), content_hash=reference["content_hash"], evidence_id=reference["id"]))
        if not file.hunks:
            key = "hunk-" + stable_hash([file.file_path, "file_metadata"])[:24]
            ids.append(key)
            hunks.append(DiffManifestHunk(id=key, file_path=file.file_path, hunk_id="file:" + file.file_path,
                line_count=0, content_hash=stable_hash(file.model_dump(mode="json"))))
        metadata.append({**file.model_dump(mode="json", exclude={"hunks"}), "hunk_ids": ids})
    manifest = UnitDiffManifest(review_unit_id=unit.id, snapshot=record["snapshot"], input_hash="",
                                files=metadata, hunks=hunks)
    manifest.input_hash = stable_hash(manifest.model_dump(mode="json", exclude={"input_hash"}))
    return manifest


def new_coverage_ledger(manifest):
    return UnitCoverageLedger(review_unit_id=manifest.review_unit_id, manifest_hash=manifest.input_hash,
        snapshot=manifest.snapshot, diff_hunks={item.id: UnitHunkCoverage() for item in manifest.hunks},
        targets={"审查 " + item["file_path"] + " 的变更": UnitCoverageCheck() for item in manifest.files})


def active_evidence_set(manifest, files, batch_id, ranges=None, context=None):
    record = build_record_input(files, context or [], None,
        manifest.snapshot["head_sha"], manifest.snapshot["base_sha"])
    references = iter(item for item in record["evidence"] if item["source"] == "diff")
    by_key = {(item.file_path, item.hunk_id): item for item in manifest.hunks}
    intervals = iter(ranges) if ranges is not None else None
    active = []
    for file in files:
        for index, hunk in enumerate(file.hunks or [None]):
            if intervals is not None:
                interval = next(intervals)
                original = by_key[(file.file_path, interval["hunk_id"])]
                start, end = interval["start_offset"], interval["end_offset"]
            else:
                identity = (DeterministicReviewPlanner.hunk_id(file.file_path, index, hunk.model_dump(mode="json"))
                            if hunk else "file:" + file.file_path)
                original = by_key[(file.file_path, identity)]
                start, end = 0, original.line_count
            if not 0 <= start <= end <= original.line_count:
                raise ValueError("active_hunk_range_outside_manifest")
            reference = next(references) if hunk else None
            active.append(UnitActiveHunk(manifest_id=original.id, start_offset=start, end_offset=end,
                evidence_id=reference["id"] if reference else None,
                content_hash=reference["content_hash"] if reference else original.content_hash,
                source_hunk_hash=original.content_hash))
    if intervals is not None and next(intervals, None) is not None:
        raise ValueError("active_hunk_inventory_mismatch")
    return UnitActiveEvidenceSet(batch_id=batch_id, manifest_hash=manifest.input_hash, hunks=active,
        supporting_evidence_ids=[item["id"] for item in record["evidence"] if item["source"] != "diff"])


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


def update_coverage(ledger, manifest, active, summary, plan=None):
    result = ledger.model_copy(deep=True)
    if plan:
        for target in [*plan.review_objectives, *plan.coverage_targets]:
            result.targets.setdefault(target, UnitCoverageCheck())
        for hypothesis in plan.risk_hypotheses:
            result.hypotheses.setdefault(hypothesis.id, UnitCoverageCheck(required=hypothesis.priority == "high"))
    current = summary.record if summary.status == "reported" and summary.latest_attempt_status in (None, "reported") else None
    trusted = {item.id for item in summary.evidence}
    checked = set()
    if current:
        for check in current.target_checks:
            status = "checked" if check.status == "checked" else "unresolved"
            result.targets[check.target] = UnitCoverageCheck(status=status, evidence_ids=check.evidence_ids, reason=check.reason)
            if status == "checked":
                checked.update(check.evidence_ids)
        for check in current.hypothesis_checks:
            prior = result.hypotheses.get(check.hypothesis_id, UnitCoverageCheck())
            if check.status == "unresolved" and not check.evidence_ids and check.reason == "模型未提供该假设的核验结果":
                result.hypotheses.setdefault(check.hypothesis_id, prior)
                continue
            result.hypotheses[check.hypothesis_id] = prior.model_copy(update={
                "status": check.status, "evidence_ids": check.evidence_ids, "reason": check.reason})
            if check.status in {"supported", "refuted"}:
                checked.update(check.evidence_ids)
        for check in current.contract_dependencies:
            key = stable_hash([check.file_path, check.symbol, check.assumption])
            result.dependencies[key] = UnitCoverageCheck(status=check.status, evidence_ids=check.evidence_ids,
                                                        reason=check.assumption)
        for question in current.unresolved_questions:
            key = question_identity(question.model_dump(mode="json"))
            result.questions[key] = UnitQuestionCoverage(question=question.question,
                affected_files=question.affected_files, evidence_ids=question.evidence_ids)
        for update in current.question_updates:
            if update.question_id in result.questions:
                result.questions[update.question_id].status = update.status
        for reference in summary.evidence:
            if reference.source != "diff":
                result.supporting_evidence[reference.id] = reference
    by_id = {item.id: item for item in manifest.hunks}
    for item in active.hunks:
        covered = result.diff_hunks[item.manifest_id]
        covered.batch_ids = list(dict.fromkeys([*covered.batch_ids, active.batch_id]))
        if current and item.evidence_id and item.evidence_id in checked & trusted:
            interval = [item.start_offset, item.end_offset]
            if interval not in covered.reviewed_ranges:
                covered.reviewed_ranges.append(interval)
            covered.evidence_ids = list(dict.fromkeys([*covered.evidence_ids, item.evidence_id]))
            covered.status = "reviewed" if _covers(covered.reviewed_ranges, 0, by_id[item.manifest_id].line_count) else "pending"
            covered.reason = None
        else:
            covered.status, covered.reason = "needs_followup", summary.reason or "active_hunk_not_checked"
    for file in manifest.files:
        target = "审查 " + file["file_path"] + " 的变更"
        if all(result.diff_hunks[identity].status == "reviewed" for identity in file["hunk_ids"]):
            result.targets[target] = UnitCoverageCheck(status="checked", evidence_ids=list(dict.fromkeys(
                identity for key in file["hunk_ids"] for identity in result.diff_hunks[key].evidence_ids)))
    return result


def coverage_gaps(manifest, ledger, active=None, plan=None):
    if (manifest is None or ledger is None or manifest.input_hash != ledger.manifest_hash
            or manifest.review_unit_id != ledger.review_unit_id or manifest.snapshot != ledger.snapshot
            or manifest.input_hash != stable_hash(manifest.model_dump(mode="json", exclude={"input_hash"}))
            or {item.id for item in manifest.hunks} != set(ledger.diff_hunks)):
        return ["coverage_manifest_mismatch"]
    if active is not None:
        if active.manifest_hash != manifest.input_hash or not active.hunks:
            return ["active_manifest_mismatch"]
        return [item.manifest_id for item in active.hunks if item.manifest_id not in ledger.diff_hunks
                or ledger.diff_hunks[item.manifest_id].status == "needs_followup"
                or not _covers(ledger.diff_hunks[item.manifest_id].reviewed_ranges, item.start_offset, item.end_offset)]
    for hunk in manifest.hunks:
        check = ledger.diff_hunks[hunk.id]
        if check.status == "reviewed" and (not check.evidence_ids or
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
    return [*[key for key, check in ledger.diff_hunks.items() if check.status != "reviewed"],
            *[key for key, check in ledger.targets.items() if check.required and (check.status != "checked" or not check.evidence_ids)],
            *[key for key, check in ledger.hypotheses.items() if check.required and (check.status not in {"supported", "refuted"} or not check.evidence_ids)],
            *[key for key, check in ledger.questions.items() if check.status == "pending"],
            *[key for key, check in ledger.dependencies.items() if check.status != "verified"]]


def rebuild_coverage(manifest, entries):
    ledger = new_coverage_ledger(manifest)
    for entry in entries:
        result = entry.result
        if result and result.active_evidence_set:
            ledger = update_coverage(ledger, manifest, result.active_evidence_set, result.review_summary, result.plan)
    return ledger


def active_body_chars(files):
    return sum(len(json.dumps(hunk.model_dump(mode="json"), ensure_ascii=False)) for file in files for hunk in file.hunks)


def hierarchy_payload(state, evidence):
    keys = ("unit_metadata", "diff_manifest", "coverage_ledger", "active_evidence_set")
    if not any(key in state for key in keys):
        return {}
    if not all(key in state for key in keys):
        raise ValueError("incomplete_unit_hierarchy")
    manifest = UnitDiffManifest.model_validate(state["diff_manifest"])
    ledger = UnitCoverageLedger.model_validate(state["coverage_ledger"])
    active = UnitActiveEvidenceSet.model_validate(state["active_evidence_set"])
    if (manifest.input_hash != stable_hash(manifest.model_dump(mode="json", exclude={"input_hash"}))
            or manifest.input_hash != ledger.manifest_hash or manifest.input_hash != active.manifest_hash
            or manifest.review_unit_id != ledger.review_unit_id or manifest.snapshot != ledger.snapshot
            or {item.id for item in manifest.hunks} != set(ledger.diff_hunks)):
        raise ValueError("unit_hierarchy_binding_mismatch")
    by_id = {item.id: item for item in manifest.hunks}
    if any(item.manifest_id not in by_id or not 0 <= item.start_offset <= item.end_offset <= by_id[item.manifest_id].line_count
           for item in active.hunks):
        raise ValueError("unit_hierarchy_active_range_mismatch")
    diffs = [item for item in evidence if item["source"] == "diff"]
    displayed = [item for item in active.hunks if item.evidence_id]
    if len(displayed) != len(diffs) or any(
        item.evidence_id != reference["id"] or item.content_hash != reference["content_hash"]
        or by_id[item.manifest_id].file_path != reference["file_path"]
        or item.source_hunk_hash != by_id[item.manifest_id].content_hash
        or (item.start_offset == 0 and item.end_offset == by_id[item.manifest_id].line_count
            and item.evidence_id != by_id[item.manifest_id].evidence_id)
        for item, reference in zip(displayed, diffs, strict=True)):
        raise ValueError("unit_hierarchy_active_evidence_mismatch")
    return {key: state[key] for key in keys}
