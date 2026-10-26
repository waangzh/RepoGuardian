"""Deterministic evidence, coverage, attribution and publication eligibility."""

import json

from app.review.evidence import digest
from app.review.models import (
    CandidateFinding, DiscoveryOutput, VerificationOutput, VerificationResult, VerifiedFinding,
)


def collect_candidates(results: list[dict]) -> list[dict]:
    candidates = {}
    for result in results:
        if result["status"] != "completed":
            continue
        output = DiscoveryOutput.model_validate(result["output"])
        for proposal in output.findings:
            text = proposal.model_dump(exclude={"evidence_ids"})
            identity = digest(json.dumps(text, sort_keys=True, ensure_ascii=False))
            if identity not in candidates:
                candidates[identity] = CandidateFinding(
                    **proposal.model_dump(), id=identity, assignment_ids=[result["id"]])
            else:
                candidate = candidates[identity]
                candidate.evidence_ids = sorted(set(candidate.evidence_ids + proposal.evidence_ids))
                candidate.assignment_ids = sorted(set(candidate.assignment_ids + [result["id"]]))
    return [candidates[key].model_dump() for key in sorted(candidates)]


def source_refs(service, identities, issued_ids):
    if not identities or not set(identities) <= set(issued_ids):
        raise ValueError("evidence_not_read_by_agent")
    return [service.resolve(identity) for identity in identities]


def candidate_gate(candidate, service, results, changed_files):
    issued = {identity for result in results if result["id"] in candidate.assignment_ids
              for identity in result["evidence_ids"]}
    refs = source_refs(service, candidate.evidence_ids, issued)
    if not any(candidate.affected_path == item["file_path"] for item in changed_files):
        raise ValueError("candidate_outside_changed_files")
    if not any(ref.path == candidate.affected_path
               and ref.start_line <= candidate.line <= ref.end_line for ref in refs):
        raise ValueError("candidate_anchor_not_read")
    return refs


def verify_finding(candidate, result, service, changed_files):
    output = VerificationOutput.model_validate(result["output"])
    refs = source_refs(service, output.evidence_ids, result["evidence_ids"])
    if output.counterevidence_ids:
        source_refs(service, output.counterevidence_ids, result["evidence_ids"])
    decision = output.decision
    if decision == "confirmed":
        if output.attribution == "uncertain" or output.attribution != candidate.attribution:
            raise ValueError("finding_attribution_unverified")
        if output.counterevidence_ids:
            raise ValueError("finding_counterevidence_unresolved")
        if not any(ref.path == candidate.affected_path
                   and ref.start_line <= candidate.line <= ref.end_line for ref in refs):
            raise ValueError("verification_anchor_not_read")
        change = next(item for item in changed_files if item["file_path"] == candidate.affected_path)
        base_path = change.get("old_file_path") or candidate.affected_path
        if change.get("change_type") != "deleted" and not any(
                ref.side == "head" and ref.path == candidate.affected_path
                and ref.start_line <= candidate.line <= ref.end_line for ref in refs):
            raise ValueError("head_failure_source_missing")
        if change.get("change_type") != "added" and not any(
                ref.side == "base" and ref.path == base_path for ref in refs):
            raise ValueError("base_comparison_missing")
    verification = VerificationResult(candidate_id=candidate.id, **output.model_dump())
    inline = None
    if decision == "confirmed":
        change = next(item for item in changed_files if item["file_path"] == candidate.affected_path)
        if any(line.get("kind") == "added" and line.get("new_line_no") == candidate.line
               for hunk in change.get("hunks", []) for line in hunk.get("lines", [])):
            if any(ref.side == "head" and ref.path == candidate.affected_path
                   and ref.start_line <= candidate.line <= ref.end_line for ref in refs):
                inline = candidate.line
        return verification, VerifiedFinding(
            id=candidate.id, candidate=candidate, verification=verification,
            evidence_ids=sorted(set(output.evidence_ids)), inline_line=inline)
    return verification, None


def covered_goals(goals, results, service, assignments):
    by_assignment = {item["id"]: set(item["goal_ids"]) for item in assignments}
    accepted = {}
    for result in results:
        if result["status"] != "completed":
            continue
        for examined in DiscoveryOutput.model_validate(result["output"]).examined:
            if examined.goal_id not in by_assignment.get(result["id"], set()):
                continue
            try:
                refs = source_refs(service, examined.evidence_ids, result["evidence_ids"])
            except (ValueError, OSError):
                continue
            accepted.setdefault(examined.goal_id, []).extend(refs)
    output = []
    for goal in goals:
        goal = dict(goal)
        intervals = sorted((ref.start_line, ref.end_line) for ref in accepted.get(goal["id"], [])
                           if ref.path == goal["path"] and ref.side == goal["side"])
        end = goal["start_line"] - 1
        for first, last in intervals:
            if first > end + 1:
                break
            end = max(end, last)
        if goal["status"] != "blocked" and end >= goal["end_line"]:
            goal["status"] = "examined"
        output.append(goal)
    return output
