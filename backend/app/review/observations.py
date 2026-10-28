"""Observations describe source changes; they never grant coverage or correctness."""

import json

from app.review.evidence import digest
from app.review.findings import source_refs
from app.review.models import ChangeObservation, DiscoveryOutput


def collect_observations(results, assignments, units, evidence):
    owners = {item["id"]: item for item in assignments if item["reviewer"] == "reviewer"}
    groups = {item["id"]: item for item in units}
    accepted, errors = {}, []
    for result in results:
        assignment = owners.get(result["id"])
        if result["status"] != "completed" or assignment is None:
            continue
        unit = groups.get(assignment["unit_id"])
        if unit is None:
            continue
        for index, proposal in enumerate(DiscoveryOutput.model_validate(result["output"]).observations):
            try:
                refs = source_refs(evidence, proposal.evidence_ids, result["evidence_ids"])
                # BASE renames are allowed through the unit's side-specific Goals.
                paths = set(unit["changed_paths"]) | set(assignment["focus_paths"])
                if not any(ref.path in paths for ref in refs):
                    raise ValueError("observation_outside_unit")
                if proposal.before and not any(ref.side == "base" for ref in refs):
                    raise ValueError("observation_before_source_missing")
                if proposal.after and not any(ref.side == "head" for ref in refs):
                    raise ValueError("observation_after_source_missing")
                payload = {**proposal.model_dump(), "unit_id": unit["id"], "assignment_id": result["id"],
                           "sources": [ref.model_dump() for ref in refs]}
                identity = digest(json.dumps(payload, sort_keys=True, ensure_ascii=False))
                accepted[identity] = ChangeObservation(id=identity, **payload)
            except (ValueError, OSError):
                errors.append(f"{result['id']}:{index}:observation_evidence_invalid")
    return [accepted[key] for key in sorted(accepted)], errors


def validated_observations(report, evidence):
    observations, _ = collect_observations(
        [item.model_dump() for item in report.discovery_results],
        [item.model_dump() for item in report.assignments],
        [item.model_dump() for item in report.review_units], evidence)
    stored = {item.id: item for item in report.observations}
    return [item for item in observations if stored.get(item.id) == item]


def observation_text(item):
    """Public prose uses neutral descriptions, not unverified impact analysis."""
    label = "测试源码变化（不表示测试已运行）：" if item.kind == "test_source" else ""
    return label + item.change


def fallback_observations(report, observations):
    """Without synthesis, quarantine observations from disputed review units."""
    if report is None:
        return list(observations)
    refuted = {item.candidate_id for item in report.decisions if item.decision == "refuted"}
    disputed_sources = {identity for candidate in report.candidates if candidate.id in refuted
                        for identity in candidate.assignment_ids}
    disputed_units = {item.unit_id for item in report.assignments if item.id in disputed_sources}
    return [item for item in observations
            if item.assignment_id not in disputed_sources and item.unit_id not in disputed_units]


def author_comment_text(verification):
    comment = verification.author_comment
    if comment:
        return comment.body or (
            f"当{comment.trigger}，{comment.cause}，{comment.impact}。\n\n{comment.recommendation}")
    # Missing prose must not restore an impact the independent verifier rejected.
    return verification.explanation
