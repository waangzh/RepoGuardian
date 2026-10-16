"""绑定被核验的原主张与发布文本；哈希只证明身份，不证明语义。"""

from app.services.fingerprints import stable_hash
from types import SimpleNamespace

from pydantic import ValidationError

from app.models.review import ReviewIssue
from app.models.review_runtime import Candidate, VerificationResult
from app.services.issue_policy import IssuePolicyService


TEXT_FIELDS = ("claim", "affected_behavior", "failure_scenario", "recommendation")


def verification_subject_hash(candidate, spec):
    return stable_hash({"head_sha": spec.snapshot_head_sha, "base_sha": spec.snapshot_base_sha,
        "candidate": candidate.model_dump(mode="json", exclude={"source_result_ids"})})


def publication_view(candidate, verification, spec, *, usage_known):
    expected = verification_subject_hash(candidate, spec)
    if verification.subject_hash != expected or verification.candidate_id != candidate.candidate_id:
        raise ValueError("publication_subject_mismatch")
    if ((verification.decision.value == "supported" and verification.publication_action not in {"retain", "narrow"})
            or (verification.decision.value != "supported" and verification.publication_action != "none")
            or (verification.publication_action == "narrow") != (verification.narrowing is not None)):
        raise ValueError("publication_decision_mismatch")
    original = {key: getattr(candidate, key) for key in TEXT_FIELDS}
    display = dict(original)
    if verification.narrowing is not None:
        for key in ("affected_behavior", "recommendation"):
            value = getattr(verification.narrowing, key)
            if value is not None:
                display[key] = value
    publishable = usage_known and verification.decision.value == "supported"
    return {"version": "claim-publication-v1", "original_candidate_id": candidate.candidate_id,
        "subject_hash": expected, "original_text": original, "verified_core": {
            "claim": candidate.claim, "failure_scenario": candidate.failure_scenario},
        "verified_text": display if verification.decision.value == "supported" else None,
        "published_text": display if publishable else None,
        "action": verification.publication_action, "decision": verification.decision.value,
        "basis": verification.narrowing.basis if verification.narrowing else verification.reason,
        "evidence_ids": verification.evidence_ids, "request_id": verification.request_id,
        "attempt_id": verification.attempt_id, "work_item_id": verification.work_item_id,
        "head_sha": spec.snapshot_head_sha, "base_sha": spec.snapshot_base_sha,
        "usage_known": usage_known}


def publication_issue(task, record):
    """重验保存的发布视图与 Issue 策略；只读且不加载目标仓库。"""
    if record.get("status") != "confirmed":
        return None, "finding_not_confirmed"
    view = record.get("publication")
    if not isinstance(view, dict) or view.get("version") != "claim-publication-v1":
        return None, "publication_contract_missing"
    if view.get("decision") != "supported":
        return None, "verification_not_supported"
    if view.get("usage_known") is not True:
        return None, "publication_usage_unknown"
    text = view.get("published_text")
    if not isinstance(text, dict) or any(not isinstance(text.get(key), str) or not text[key].strip() for key in TEXT_FIELDS):
        return None, "publication_text_missing"
    manifest = task.run_manifest
    if not manifest or (view.get("head_sha"), view.get("base_sha")) != (manifest.head_sha, manifest.effective_base_sha):
        return None, "snapshot_mismatch"
    try:
        candidate = Candidate.model_validate(record.get("candidate"))
        verification = VerificationResult.model_validate(record.get("verification"))
        spec = SimpleNamespace(snapshot_head_sha=manifest.head_sha, snapshot_base_sha=manifest.effective_base_sha)
        if verification.snapshot_head_sha != manifest.head_sha or publication_view(candidate, verification, spec, usage_known=True) != view:
            return None, "publication_subject_mismatch"
    except (ValidationError, ValueError):
        return None, "publication_subject_mismatch"
    try:
        issue = ReviewIssue.model_validate(record.get("issue"))
    except ValidationError:
        return None, "issue_policy_failed"
    if issue.placement not in {"inline", "summary"}:
        return None, "placement_not_publishable"
    check = IssuePolicyService().check(issue, task.review_units)
    if not check.passed or issue.id != candidate.candidate_id:
        return None, "issue_policy_failed"
    if (issue.title != text["claim"] or any(getattr(issue, key) != text[key] for key in TEXT_FIELDS[1:])):
        return None, "publication_subject_mismatch"
    location = issue.resolved_location
    evidence = issue.primary_evidence
    if issue.placement == "inline" and (not location or
            (location.file_path, location.start_line, location.end_line, location.side) !=
            (evidence.file_path, evidence.resolved_start_line, evidence.resolved_end_line, evidence.resolved_side)):
        return None, "issue_policy_failed"
    return issue.model_copy(update={"severity": check.normalized_severity}), None
