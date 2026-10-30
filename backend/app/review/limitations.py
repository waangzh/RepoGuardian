"""Source-bound reconciliation of discovery limitations and final verification."""

import json

from app.review.evidence import digest, has_structural_source
from app.review.models import (
    CandidateLimitationResolution, DiscoveryGap, LimitationResolution, LimitationReviewOutput,
    ReviewLimitation, VerificationOutput, VerificationResult,
)

ASSESSMENT_ERROR_CODES = frozenset({
    "limitation_assessment_members_mismatch", "limitation_assessment_goal_outside_scope",
    "limitation_assessment_evidence_not_read", "limitation_assessment_change_source_missing",
    "limitation_assessment_structural_source_missing", "limitation_assessment_source_invalid",
})


def _limitations(texts, source, source_id, candidate_ids, goal_ids=()):
    records = {}
    for text in texts:
        identity = digest(json.dumps([source, source_id, text], ensure_ascii=False))
        records[identity] = ReviewLimitation(id=identity, source=source, source_id=source_id,
            text=text, candidate_ids=sorted(candidate_ids), goal_ids=sorted(goal_ids))
    return list(records.values())


def discovery_limitations(results, candidates, assignments=()):
    records = []
    goals = {item["id"]: item["goal_ids"] for item in assignments}
    for result in results:
        related = [item["id"] for item in candidates if result["id"] in item["assignment_ids"]]
        allowed = goals.get(result["id"], [])
        submitted = result.get("output", {}).get("limitations", [])
        declarations = (submitted if any(isinstance(item, dict) for item in submitted)
                        else result.get("investigation_limitations", []))
        for declaration in declarations:
            if isinstance(declaration, dict):
                gap = DiscoveryGap.model_validate(declaration)
                identity = digest(json.dumps(["discovery", result["id"], gap.model_dump()],
                                             sort_keys=True, ensure_ascii=False))
                records.append(ReviewLimitation(id=identity, source="discovery", source_id=result["id"],
                    text=gap.missing_fact, candidate_ids=sorted(related),
                    goal_ids=sorted(set(gap.goal_ids) & set(allowed)), status="reported", gap=gap))
            else:
                records.extend(_limitations([declaration], "discovery", result["id"], related, allowed))
    return records


def candidate_limitations(state, candidates):
    identities = {item["id"] for item in candidates}
    return [record.model_dump() for record in discovery_limitations(
        state.get("discovery_results", []), state.get("candidates", []), state.get("assignments", []))
        if identities.intersection(record.candidate_ids)]


def validate_resolutions(resolutions, limitations, candidate_id, issued_ids, service):
    allowed = {item["id"]: item for item in limitations}
    seen = set()
    for resolution in resolutions:
        record = allowed.get(resolution.limitation_id)
        if (record is None or candidate_id not in record["candidate_ids"]
                or resolution.limitation_id in seen):
            raise ValueError("limitation_resolution_outside_scope")
        seen.add(resolution.limitation_id)
        if not set(resolution.evidence_ids) <= set(issued_ids):
            raise ValueError("limitation_resolution_evidence_not_read")
        for identity in resolution.evidence_ids:
            service.resolve(identity)


def assessment_errors(output, limitations, goals, issued_ids, service):
    expected = {item["id"]: item for item in limitations}
    supplied = [item.limitation_id for item in output.assessments]
    if len(supplied) != len(set(supplied)) or set(supplied) != set(expected):
        raise ValueError("limitation_assessment_members_mismatch")
    goal_map = {goal["id"]: goal for goal in goals}
    errors = []
    for index, assessment in enumerate(output.assessments):
        allowed = set(expected[assessment.limitation_id]["goal_ids"])
        invalid = set(assessment.evidence_ids) - set(issued_ids)
        code = None
        if not set(assessment.goal_ids) <= allowed or not set(assessment.goal_ids) <= set(goal_map):
            code = "limitation_assessment_goal_outside_scope"
        elif invalid:
            code = "limitation_assessment_evidence_not_read"
        else:
            try:
                refs = [service.resolve(identity) for identity in assessment.evidence_ids]
            except (ValueError, OSError):
                code = "limitation_assessment_source_invalid"
            else:
                source_goals = [identity for identity in assessment.goal_ids
                                if goal_map[identity].get("kind") != "structural"]
                if source_goals and not any(
                        ref.path == goal_map[identity]["path"] and ref.side == goal_map[identity]["side"]
                        for identity in source_goals for ref in refs):
                    code = "limitation_assessment_change_source_missing"
                if any(goal_map[identity].get("kind") == "structural"
                       and not has_structural_source(goal_map[identity], refs)
                       for identity in assessment.goal_ids):
                    code = "limitation_assessment_structural_source_missing"
        if code:
            errors.append({"index": index, "limitation_id": assessment.limitation_id, "code": code,
                           "invalid_evidence_ids": sorted(invalid), "allowed_goal_ids": sorted(allowed)})
    return errors


def validate_assessments(output, limitations, goals, issued_ids, service):
    errors = assessment_errors(output, limitations, goals, issued_ids, service)
    if errors:
        raise ValueError(errors[0]["code"])


def limitation_discoveries(results):
    return [{**item, "output": {"findings": item["output"].get("findings", [])}}
            for item in results if item["status"] == "completed"]


def review_limitations(report, service=None):
    """Missing, conflicting or invalid assessments keep the original gap open."""
    data = report if isinstance(report, dict) else report.model_dump()
    candidates = data.get("candidates", [])
    assignments = data.get("assignments", [])
    goals = data.get("goals", [])
    records = discovery_limitations(data.get("discovery_results", []), candidates, assignments)
    by_assignment = {item["id"]: item["goal_ids"] for item in assignments}
    for decision in data.get("decisions", []):
        candidate = next((item for item in candidates if item["id"] == decision["candidate_id"]), {})
        goal_ids = {goal for identity in candidate.get("assignment_ids", [])
                    for goal in by_assignment.get(identity, [])}
        verification_gaps = _limitations(decision.get("limitations", []), "verification",
                                         decision["candidate_id"], [decision["candidate_id"]], goal_ids)
        if data.get("policy_version") == 2:
            for record in verification_gaps:
                record.status = "reported"
        records.extend(verification_gaps)
    by_id = {item.id: item for item in records}
    for result in data.get("limitation_results", []):
        if service is not None:
            _apply_assessments(result, by_id, goals, candidates, service)
    allowed = [item.model_dump() for item in records]
    results = {item["id"]: item for item in (
        data.get("verification_results", []) + data.get("investigation_results", []))
        if item["status"] == "completed"}
    assessments = {}
    for raw in data.get("decisions", []):
        decision = VerificationResult.model_validate(raw)
        result = results.get(decision.candidate_id)
        if service is None or result is None:
            continue
        try:
            output = VerificationOutput.model_validate(result["output"])
            if output.model_dump() != decision.model_dump(exclude={"candidate_id"}):
                continue
            validate_resolutions(output.limitation_resolutions, allowed, decision.candidate_id,
                                 result["evidence_ids"], service)
        except (ValueError, OSError):
            continue
        for resolution in output.limitation_resolutions:
            assessments.setdefault(resolution.limitation_id, []).append((decision.candidate_id, resolution))
    for record in records:
        claims = assessments.get(record.id, [])
        record.resolution_claims = [CandidateLimitationResolution(
            candidate_id=identity, **claim.model_dump()) for identity, claim in claims]
        # Candidate-local judgments cannot erase an independent necessary gap.
        if record.assessment is not None:
            continue
        covered = {identity for identity, _ in claims}
        if not claims or not set(record.candidate_ids) <= covered:
            continue
        if len({claim.status for _, claim in claims}) == 1:
            record.resolved_by, record.resolution = claims[0]
            record.status = record.resolution.status
        else:
            # Both categories are explicitly nonblocking, for different reasons.
            record.status = "nonblocking"
    return records


def _apply_assessments(result, by_id, goals, candidates, service):
    # Each attempt retains its own issued IDs; a retry never inherits read permission.
    for attempt in [*result.get("previous_attempts", []), result]:
        if attempt.get("status") != "completed" and attempt.get("error") not in ASSESSMENT_ERROR_CODES:
            continue
        try:
            output = LimitationReviewOutput.model_validate(attempt.get("output", {}))
            members = sorted({item.limitation_id for item in output.assessments})
            if result["id"] != "scope-" + digest(":".join(members)):
                continue
            selected = [by_id[identity].model_dump() for identity in members]
            errors = assessment_errors(output, selected, goals, attempt["evidence_ids"], service)
        except (KeyError, ValueError, OSError):
            continue
        invalid = {item["limitation_id"] for item in errors}
        for assessment in output.assessments:
            if assessment.limitation_id in invalid:
                continue
            record = by_id[assessment.limitation_id]
            record.status, record.assessment = assessment.status, assessment
            record.resolved_by = result["id"] if assessment.status != "unresolved" else None
            record.resolution = (LimitationResolution.model_validate(
                assessment.model_dump(include=set(LimitationResolution.model_fields)))
                if assessment.status != "unresolved" else None)
            new_candidates = {item["id"] for item in candidates if result["id"] in item["assignment_ids"]}
            record.candidate_ids = sorted(set(record.candidate_ids) | new_candidates)
