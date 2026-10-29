"""Observations describe source changes; they never grant coverage or correctness."""

import json

from app.review.evidence import digest
from app.review.findings import source_refs
from app.review.models import ChangeObservation, DiscoveryOutput


def validate_observation(proposal, evidence, issued_ids, paths, goals=()):
    refs = source_refs(evidence, proposal.evidence_ids, issued_ids)
    relevant = [ref for ref in refs if ref.path in paths]
    if not relevant:
        raise ValueError("observation_outside_unit")
    if proposal.basis == "head_state" and (proposal.before or not any(
            ref.side == "head" for ref in relevant)):
        raise ValueError("observation_head_state_invalid")
    if proposal.basis == "change":
        for path in {ref.path for ref in relevant}:
            required = {goal["side"] for goal in goals
                        if goal["path"] == path and goal.get("kind", "source") == "source"}
            required = required or {"base", "head"}
            if not required <= {ref.side for ref in relevant if ref.path == path}:
                raise ValueError("observation_comparison_source_missing")
    if proposal.before and not any(ref.side == "base" for ref in relevant):
        raise ValueError("observation_before_source_missing")
    if proposal.after and not any(ref.side == "head" for ref in relevant):
        raise ValueError("observation_after_source_missing")
    return refs


def collect_observations(results, assignments, units, evidence, goals=()):
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
                # BASE renames are allowed through the unit's side-specific Goals.
                paths = set(unit["changed_paths"]) | set(assignment["focus_paths"])
                selected = [goal for goal in goals if goal["id"] in assignment["goal_ids"]]
                paths |= {goal["path"] for goal in selected}
                refs = validate_observation(proposal, evidence, result["evidence_ids"], paths, selected)
                payload = {**proposal.model_dump(), "unit_id": unit["id"], "assignment_id": result["id"],
                           "sources": [ref.model_dump() for ref in refs]}
                identity = digest(json.dumps(payload, sort_keys=True, ensure_ascii=False))
                accepted[identity] = ChangeObservation(id=identity, **payload)
            except (ValueError, OSError) as exc:
                code = str(exc) if isinstance(exc, ValueError) else "observation_source_unavailable"
                errors.append(f"{result['id']}:{index}:{code}")
    return [accepted[key] for key in sorted(accepted)], errors


def validated_observations(report, evidence):
    observations, _ = collect_observations(
        [item.model_dump() for item in report.discovery_results],
        [item.model_dump() for item in report.assignments],
        [item.model_dump() for item in report.review_units], evidence,
        [item.model_dump() for item in report.goals])
    stored = {item.id: item for item in report.observations}
    return [item for item in observations if stored.get(item.id) == item]


def author_comment_text(verification):
    comment = verification.author_comment
    if comment:
        return comment.body or (
            f"当{comment.trigger}，{comment.cause}，{comment.impact}。\n\n{comment.recommendation}")
    # Missing prose must not restore an impact the independent verifier rejected.
    return verification.explanation
