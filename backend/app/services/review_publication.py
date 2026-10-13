"""绑定被核验的原主张与发布文本；哈希只证明身份，不证明语义。"""

from app.services.fingerprints import stable_hash


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
