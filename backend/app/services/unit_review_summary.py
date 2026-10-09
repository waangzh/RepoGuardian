"""Unit 检查记录的证据目录、范围核验与保守补全。"""

import json
from typing import Any

from app.models.review import (
    ChangedFile, UnitEvidenceReference, UnitHypothesisCheck, UnitReviewRecord,
    UnitReviewSummary, UnitTargetCheck,
)
from app.services.review_input_context import build_context_evidence, make_evidence, EVIDENCE_INPUT_VERSION, input_snapshot, question_identity
from app.review.input_protocol import CANONICAL_UNIT_INPUT_PROTOCOL
from app.services.file_change_evidence import file_change_reference, set_file_change_impact_scope


def build_record_input(
    files: list[ChangedFile], context: list[dict[str, Any]],
    plan: Any, head_sha: str, base_sha: str = "",
) -> dict[str, Any]:
    targets = list(dict.fromkeys(
        [*(plan.review_objectives if plan else []), *(plan.coverage_targets if plan else [])]
    )) or [f"审查 {item.file_path} 的变更" for item in files]
    evidence: list[dict[str, Any]] = []

    for item in files:
        for hunk in item.hunks:
            content = json.dumps(
                hunk.model_dump(), ensure_ascii=False, sort_keys=True,
            )
            evidence.append(make_evidence(item.file_path, "diff", hunk.new_start,
                hunk.new_start + max(0, hunk.new_length - 1), content, head_sha, base_sha))
    chunks, context_evidence = build_context_evidence(context, head_sha, base_sha)
    evidence.extend(reference for file in files
                    if (reference := file_change_reference(file, head_sha, base_sha)) is not None)
    evidence.extend(context_evidence)
    result = {
        "targets": targets,
        "input_protocol": CANONICAL_UNIT_INPUT_PROTOCOL,
        "hypotheses": [item.model_dump(mode="json") for item in plan.risk_hypotheses] if plan else [],
        "evidence": evidence,
        "readonly_context": chunks,
        "snapshot": {"head_sha": head_sha, "base_sha": base_sha, "input_version": EVIDENCE_INPUT_VERSION},
        "context_coverage": {"source_snippets": len(context), "included_chunks": len(chunks),
                             "rendered_diff_snippets": sum(item.get("source") == "file_read_diff" for item in context)},
    }
    set_file_change_impact_scope(result)
    return result


def validate_record(
    record: UnitReviewRecord | None, record_input: dict[str, Any],
    readable_files: set[str], error: str | None = None,
) -> UnitReviewSummary:
    catalog = [UnitEvidenceReference.model_validate({
        key: value for key, value in item.items() if key != "content"
    }) for item in record_input["evidence"]]
    snapshot = input_snapshot(record_input)
    common = {"evidence": catalog, "input_protocol": record_input.get("input_protocol") or CANONICAL_UNIT_INPUT_PROTOCOL,
              "latest_attempt_snapshot": snapshot}
    if common["input_protocol"] != CANONICAL_UNIT_INPUT_PROTOCOL:
        return UnitReviewSummary(**common, reason="legacy_provider_without_canonical_evidence_protocol",
            latest_attempt_status="unknown", latest_attempt_reason="legacy_provider_without_canonical_evidence_protocol")
    if record is None:
        reason = error or "missing_review_record"
        return UnitReviewSummary(**common, reason=reason, latest_attempt_reason=reason,
                                 latest_attempt_status="invalid" if error else "missing")
    try:
        if len(record.contract_dependencies) > 20 or len(record.unresolved_questions) > 20:
            raise ValueError("one diagnosis record exceeds its 20-item limit")
        by_id = {item.id: item for item in catalog}
        allowed_targets = set(record_input["targets"])
        allowed_hypotheses = {item["id"] for item in record_input["hypotheses"]}
        if any(item.target not in allowed_targets for item in record.target_checks):
            raise ValueError("unknown review target")
        if any(item.hypothesis_id not in allowed_hypotheses for item in record.hypothesis_checks):
            raise ValueError("unknown risk hypothesis")
        items = [*record.target_checks, *record.hypothesis_checks,
                 *record.contract_dependencies, *record.unresolved_questions, *record.question_updates]
        for item in items:
            if any(identity not in by_id for identity in item.evidence_ids):
                raise ValueError("unknown or stale evidence reference")
        for check in record.file_change_checks:
            reference = by_id.get(check.evidence_id)
            if reference is None or reference.source != "file_change":
                raise ValueError("file change check requires current metadata evidence")
            if any(identity not in by_id or by_id[identity].source == "file_change"
                   or by_id[identity].file_path not in readable_files
                   or by_id[identity].file_path not in record_input.get("file_change_impact_scope", {}).get(check.evidence_id, [])
                   for identity in check.evidence_ids):
                raise ValueError("impact check requires current readable code evidence")
            if check.impact_status == "checked" and not check.evidence_ids:
                raise ValueError("metadata alone cannot prove impact was checked")
        for dependency in record.contract_dependencies:
            if dependency.file_path not in readable_files:
                raise ValueError("contract dependency outside readable scope")
            if dependency.status != "unresolved" and not any(
                by_id[identity].file_path == dependency.file_path for identity in dependency.evidence_ids
            ):
                raise ValueError("contract evidence does not cover dependency file")
        for question in record.unresolved_questions:
            if set(question.affected_files) - readable_files:
                raise ValueError("question outside readable scope")
        known_questions = {question_identity(item): item for item in
                           (record_input.get("working_memory") or {}).get("unresolved_questions", [])}
        questions, aliases = [], {}
        for question in record.unresolved_questions:
            if question.id in known_questions:
                previous_question = known_questions[question.id]
                if (question.question != previous_question["question"]
                        or set(question.affected_files) != set(previous_question.get("affected_files") or [])):
                    raise ValueError("prior question identity cannot name a different question; use superseded")
            identity = question.id if question.id in known_questions else question_identity(
                question.model_dump(exclude={"id"}))
            if question.id:
                if question.id in aliases:
                    raise ValueError("duplicate question alias")
                aliases[question.id] = identity
            questions.append(question.model_copy(update={"id": identity}))
        if len({item.id for item in questions}) != len(questions):
            raise ValueError("duplicate question identity")
        retiring = {item.question_id for item in record.question_updates}
        replacements = (set(known_questions) | {item.id for item in questions}) - retiring
        question_updates = []
        for update in record.question_updates:
            if update.question_id not in known_questions:
                raise ValueError("question update requires a visible prior question")
            replacement = aliases.get(update.replacement_id, update.replacement_id)
            if update.status == "superseded" and replacement not in replacements:
                raise ValueError("superseded question requires an active replacement")
            question_updates.append(update.model_copy(update={"replacement_id": replacement}))
        targets = {item.target: item for item in record.target_checks}
        hypotheses = {item.hypothesis_id: item for item in record.hypothesis_checks}
        normalized = record.model_copy(update={
            "unresolved_questions": [item for item in questions if item.id not in retiring],
            "question_updates": question_updates,
            "target_checks": [targets.get(target) or UnitTargetCheck(
                target=target, status="not_checked", reason="模型未提供该目标的检查记录",
            ) for target in record_input["targets"]],
            "hypothesis_checks": [hypotheses.get(item["id"]) or UnitHypothesisCheck(
                hypothesis_id=item["id"], status="unresolved", reason="模型未提供该假设的核验结果",
            ) for item in record_input["hypotheses"]],
        })
        return UnitReviewSummary(**common, status="reported", record=normalized,
                                 last_valid_record=normalized, last_valid_snapshot=snapshot,
                                 latest_attempt_status="reported", latest_attempt_reason="references_validated_not_correctness_proof",
                                 reason="references_validated_not_correctness_proof")
    except ValueError as exc:
        reason = f"invalid_review_record: {exc}"
        return UnitReviewSummary(**common, reason=reason, latest_attempt_status="invalid",
                                 latest_attempt_reason=reason)


def merge_review_summaries(
    previous: UnitReviewSummary, current: UnitReviewSummary, declared: UnitReviewRecord | None,
) -> UnitReviewSummary:
    """保留多轮证据；缺省目标不抹掉前轮检查，显式新结论优先。"""
    by_id = {item.id: item for item in current.evidence}
    old = previous.last_valid_record or previous.record
    references = {identity for check in [*(old.target_checks if old else []),
        *(old.hypothesis_checks if old else []), *(old.contract_dependencies if old else []),
        *(old.unresolved_questions if old else [])] for identity in check.evidence_ids}
    references.update(identity for check in (old.file_change_checks if old else [])
                      for identity in [check.evidence_id, *check.evidence_ids])
    trusted = {item.id: item for item in previous.evidence}
    compatible = (bool(previous.last_valid_snapshot.get("head_sha"))
                  and previous.last_valid_snapshot.get("input_version") == EVIDENCE_INPUT_VERSION
                  and previous.last_valid_snapshot == current.latest_attempt_snapshot
                  and current.input_protocol == CANONICAL_UNIT_INPUT_PROTOCOL
                  and all(identity in by_id and trusted.get(identity) == by_id[identity] for identity in references))
    history = list(previous.record_history) if compatible else []
    if not history and old is not None and compatible:
        history.append(old)
    if current.record is not None:
        history.append(current.record)
    updates: dict[str, Any] = {"record_history": history[-3:]}
    if current.record is None and compatible and old is not None:
        updates.update(last_valid_record=old, last_valid_snapshot=previous.last_valid_snapshot)
    if old is not None and compatible and current.record is not None and declared is not None:
        old_targets = {item.target: item for item in old.target_checks}
        old_hypotheses = {item.hypothesis_id: item for item in old.hypothesis_checks}
        explicit_targets = {item.target for item in declared.target_checks}
        explicit_hypotheses = {item.hypothesis_id for item in declared.hypothesis_checks}
        file_changes = {item.evidence_id: item for item in [*old.file_change_checks, *current.record.file_change_checks]}
        dependencies = {(item.file_path, item.symbol, item.assumption): item for item in [
            *old.contract_dependencies, *current.record.contract_dependencies,
        ]}
        questions = {question_identity(item): item.model_copy(update={"id": question_identity(item)}) for item in [
            *old.unresolved_questions, *current.record.unresolved_questions,
        ]}
        for update in current.record.question_updates:
            questions.pop(update.question_id, None)
        updates["record"] = current.record.model_copy(update={
            "target_checks": [old_targets.get(item.target, item) if item.target not in explicit_targets else item
                              for item in current.record.target_checks],
            "hypothesis_checks": [old_hypotheses.get(item.hypothesis_id, item)
                                  if item.hypothesis_id not in explicit_hypotheses else item
                                  for item in current.record.hypothesis_checks],
            "contract_dependencies": list(dependencies.values()),
            "unresolved_questions": list(questions.values()),
            "file_change_checks": list(file_changes.values()),
        })
        # History is also a recoverable projection. Store the validated merged
        # state, so a later missing record cannot hide early checks on reload.
        updates["record_history"] = [*history[:-1], updates["record"]][-3:]
        updates["last_valid_record"] = updates["record"]
    return current.model_copy(update=updates)
