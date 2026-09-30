"""Unit 检查记录的证据目录、范围核验与保守补全。"""

import hashlib
import json
from typing import Any

from app.models.review import (
    ChangedFile, UnitEvidenceReference, UnitHypothesisCheck, UnitReviewRecord,
    UnitReviewSummary, UnitTargetCheck,
)


def build_record_input(
    files: list[ChangedFile], context: list[dict[str, Any]],
    plan: Any, head_sha: str, base_sha: str = "",
) -> dict[str, Any]:
    targets = list(dict.fromkeys(
        [*(plan.review_objectives if plan else []), *(plan.coverage_targets if plan else [])]
    )) or [f"审查 {item.file_path} 的变更" for item in files]
    evidence: list[dict[str, Any]] = []

    def add(path: str, source: str, start: int, end: int, content: str) -> None:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        identity = json.dumps([base_sha, head_sha, path, source, start, end, digest], ensure_ascii=False)
        reference = UnitEvidenceReference(
            id="evidence-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
            file_path=path, source=source, start_line=max(0, start),
            end_line=max(0, start, end), head_sha=head_sha, base_sha=base_sha, content_hash=digest,
        )
        evidence.append({**reference.model_dump(mode="json"), "content": content})

    for item in files:
        for hunk in item.hunks:
            content = json.dumps(
                hunk.model_dump(exclude={"removed_lines"}), ensure_ascii=False, sort_keys=True,
            )
            add(item.file_path, "diff", hunk.new_start,
                hunk.new_start + max(0, hunk.new_length - 1), content)
    for snippet in context[-20:]:
        content = str(snippet.get("content") or "")[:1_000]
        if content:
            start = int(snippet.get("start_line") or 1)
            add(str(snippet["file"]), "context", start,
                min(int(snippet.get("end_line") or start), start + len(content.splitlines()) - 1),
                content)
    return {
        "targets": targets,
        "hypotheses": [item.model_dump(mode="json") for item in plan.risk_hypotheses] if plan else [],
        "evidence": evidence,
    }


def validate_record(
    record: UnitReviewRecord | None, record_input: dict[str, Any],
    readable_files: set[str], error: str | None = None,
) -> UnitReviewSummary:
    catalog = [UnitEvidenceReference.model_validate({
        key: value for key, value in item.items() if key != "content"
    }) for item in record_input["evidence"]]
    if record is None:
        return UnitReviewSummary(evidence=catalog, reason=error or "missing_review_record")
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
                 *record.contract_dependencies, *record.unresolved_questions]
        for item in items:
            if any(identity not in by_id for identity in item.evidence_ids):
                raise ValueError("unknown or stale evidence reference")
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
        targets = {item.target: item for item in record.target_checks}
        hypotheses = {item.hypothesis_id: item for item in record.hypothesis_checks}
        normalized = record.model_copy(update={
            "target_checks": [targets.get(target) or UnitTargetCheck(
                target=target, status="not_checked", reason="模型未提供该目标的检查记录",
            ) for target in record_input["targets"]],
            "hypothesis_checks": [hypotheses.get(item["id"]) or UnitHypothesisCheck(
                hypothesis_id=item["id"], status="unresolved", reason="模型未提供该假设的核验结果",
            ) for item in record_input["hypotheses"]],
        })
        return UnitReviewSummary(status="reported", record=normalized, evidence=catalog,
                                 reason="references_validated_not_correctness_proof")
    except ValueError as exc:
        return UnitReviewSummary(evidence=catalog, reason=f"invalid_review_record: {exc}")


def merge_review_summaries(
    previous: UnitReviewSummary, current: UnitReviewSummary, declared: UnitReviewRecord | None,
) -> UnitReviewSummary:
    """保留多轮证据；缺省目标不抹掉前轮检查，显式新结论优先。"""
    evidence = {item.id: item for item in [*previous.evidence, *current.evidence]}
    history = list(previous.record_history)
    if not history and previous.record is not None:
        history.append(previous.record)
    if current.record is not None:
        history.append(current.record)
    updates: dict[str, Any] = {"evidence": list(evidence.values()), "record_history": history[-3:]}
    if previous.record is not None and current.record is not None and declared is not None:
        old_targets = {item.target: item for item in previous.record.target_checks}
        old_hypotheses = {item.hypothesis_id: item for item in previous.record.hypothesis_checks}
        explicit_targets = {item.target for item in declared.target_checks}
        explicit_hypotheses = {item.hypothesis_id for item in declared.hypothesis_checks}
        dependencies = {(item.file_path, item.symbol, item.assumption): item for item in [
            *previous.record.contract_dependencies, *current.record.contract_dependencies,
        ]}
        questions = {(item.question, tuple(item.affected_files)): item for item in [
            *previous.record.unresolved_questions, *current.record.unresolved_questions,
        ]}
        updates["record"] = current.record.model_copy(update={
            "target_checks": [old_targets.get(item.target, item) if item.target not in explicit_targets else item
                              for item in current.record.target_checks],
            "hypothesis_checks": [old_hypotheses.get(item.hypothesis_id, item)
                                  if item.hypothesis_id not in explicit_hypotheses else item
                                  for item in current.record.hypothesis_checks],
            "contract_dependencies": list(dependencies.values()),
            "unresolved_questions": list(questions.values()),
        })
    return current.model_copy(update=updates)
