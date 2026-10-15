"""Unit 检查记录的证据目录、范围核验与保守补全。"""

import json
from typing import Any

from app.models.review import (
    ChangedFile, UnitContractDependency, UnitEvidenceReference, UnitHypothesisCheck,
    UnitReviewRecord, UnitReviewSummary, UnitTargetCheck,
)
from app.services.review_input_context import build_context_evidence, make_evidence, EVIDENCE_INPUT_VERSION, input_snapshot, question_identity
from app.review.input_protocol import CANONICAL_UNIT_INPUT_PROTOCOL
from app.services.file_change_evidence import file_change_reference, set_file_change_impact_scope
from app.services.fingerprints import stable_hash


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


def review_target_catalog(targets):
    return [{"id": "target-" + stable_hash(target)[:24], "target": target} for target in targets]


def review_update_catalog(record_input):
    """两种身份空间分别公开，父问题 ID 不获得本地问题更新权限。"""
    memory = record_input.get("working_memory") or {}
    return {
        "question_updates": [{"question_id": question_identity(item), "statuses": ["resolved", "superseded"]}
                             for item in memory.get("unresolved_questions", [])],
        "obligation_updates": [{"obligation_id": item["id"], "kind": item["kind"],
            "status": "verified" if item["kind"] == "dependency" else "resolved",
            "required_files": ([item["check"]["file_path"]] if item["kind"] == "dependency"
                               else item["check"]["affected_files"])}
            for item in record_input.get("origin_obligations", [])],
    }


def normalize_review_targets(record, record_input):
    if record is None:
        return None
    target_ids = {item["id"]: item["target"] for item in record_input.get("target_catalog", [])}
    return record.model_copy(update={"target_checks": [check.model_copy(update={
        "target": target_ids.get(check.target, check.target)}) for check in record.target_checks]})


def _dependency_evidence_covers(
    reference: UnitEvidenceReference,
    dependency: UnitContractDependency,
    prior_bindings: dict[str, UnitEvidenceReference],
) -> bool:
    """契约证据必须来自当前快照的同文件，或可追溯到该快照已授权的原证据。

    携带的跨批次证据保留 derived_from_evidence_id 绑定与快照；整段携带时内容哈希与
    原证据一致，超过单条上限的正文由服务端按行分块携带，块仍属于同一原证据。
    """
    if reference.file_path != dependency.file_path:
        return False
    derived = reference.derived_from_evidence_id
    if derived is None:
        return True
    prior = prior_bindings.get(derived)
    return bool(
        prior is not None
        and prior.source == "diff"
        and prior.file_path == dependency.file_path
        and prior.head_sha == reference.head_sha
        and prior.base_sha == reference.base_sha
    )


_QUESTION_IDENTITY_ERRORS = {
    "prior question identity cannot name a different question; use superseded":
        "question_identity_conflict",
    "closed question identity cannot be reused": "closed_question_reuse",
    "question update requires a visible prior question": "unknown_question_update",
    "superseded question requires an active replacement": "missing_question_replacement",
}


def domain_output_errors(reason: str | None) -> list[dict[str, Any]]:
    """把可修复的领域校验错误翻译成有界纠错反馈；不认识的错误保持拒绝。"""
    prefix = "invalid_review_record: "
    if not reason or not reason.startswith(prefix):
        return []
    detail = reason[len(prefix):]
    if detail == "unknown review target":
        return [{"type": "unknown_target", "loc": ["review_record", "target_checks"]}]
    kind = _QUESTION_IDENTITY_ERRORS.get(detail)
    if kind:
        field = "question_updates" if kind in {"unknown_question_update", "missing_question_replacement"} else "unresolved_questions"
        return [{"type": kind, "loc": ["review_record", field],
                 "message": detail + ("; parent obligations use obligation_updates with a bound obligation_id"
                                      if kind == "unknown_question_update" else "")}]
    if detail in {"unknown origin obligation", "obligation closure requires evidence for every bound file"}:
        return [{"type": "invalid_obligation_update", "loc": ["review_record", "obligation_updates"],
                 "message": detail}]
    return []


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
        record = normalize_review_targets(record, record_input)
        if len({check.target for check in record.target_checks}) != len(record.target_checks):
            raise ValueError("duplicate review target identity")
        if len(record.contract_dependencies) > 20 or len(record.unresolved_questions) > 20:
            raise ValueError("one diagnosis record exceeds its 20-item limit")
        by_id = {item.id: item for item in catalog}
        prior_bindings = {
            item["id"]: UnitEvidenceReference.model_validate(item)
            for item in record_input.get("prior_evidence_bindings") or []
            if isinstance(item, dict) and item.get("id")
        }
        allowed_targets = set(record_input["targets"])
        allowed_hypotheses = {item["id"] for item in record_input["hypotheses"]}
        if any(item.target not in allowed_targets for item in record.target_checks):
            raise ValueError("unknown review target")
        if any(item.hypothesis_id not in allowed_hypotheses for item in record.hypothesis_checks):
            raise ValueError("unknown risk hypothesis")
        items = [*record.target_checks, *record.hypothesis_checks,
                 *record.contract_dependencies, *record.unresolved_questions, *record.question_updates, *record.obligation_updates]
        for item in items:
            if any(identity not in by_id for identity in item.evidence_ids):
                raise ValueError("unknown or stale evidence reference")
        from app.services.followup_contracts import validate_obligation_updates
        validate_obligation_updates(record, record_input, by_id)
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
                _dependency_evidence_covers(by_id[identity], dependency, prior_bindings)
                for identity in dependency.evidence_ids
            ):
                raise ValueError("contract evidence does not cover dependency file")
        for question in record.unresolved_questions:
            if set(question.affected_files) - readable_files:
                raise ValueError("question outside readable scope")
        known_questions = {question_identity(item): item for item in
                           (record_input.get("working_memory") or {}).get("unresolved_questions", [])}
        memory = record_input.get("working_memory") or {}
        closed = {item["question_id"]: item for item in memory.get("closed_question_updates", [])} if (
            not memory.get("memory_invalidated") and memory.get("question_catalog_snapshot") == snapshot
        ) else {}
        questions, aliases = [], {}
        for question in record.unresolved_questions:
            if question.id in closed:
                raise ValueError("closed question identity cannot be reused")
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
            if update.question_id in closed:
                if update.model_dump(mode="json") != closed[update.question_id]:
                    raise ValueError("closed question update must match its validated transition")
                continue  # 同快照、同证据、逐字段相同的已完成转移不再次应用。
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
