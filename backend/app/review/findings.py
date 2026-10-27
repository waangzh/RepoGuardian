"""Deterministic evidence, coverage, attribution and publication eligibility."""

import json

from app.review.evidence import digest
from app.review.models import (
    CandidateFinding, CandidateProposal, DiscoveryOutput, VerificationOutput, VerificationResult, VerifiedFinding,
)


def candidate_identity(candidate):
    fields = set(CandidateProposal.model_fields) - {"evidence_ids"}
    return digest(json.dumps(candidate.model_dump(include=fields), sort_keys=True, ensure_ascii=False))


def collect_candidates(results: list[dict]) -> list[dict]:
    candidates = {}
    for result in results:
        if result["status"] != "completed":
            continue
        output = DiscoveryOutput.model_validate(result["output"])
        for proposal in output.findings:
            identity = candidate_identity(proposal)
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
              and result["status"] == "completed"
              for identity in result["evidence_ids"]}
    refs = source_refs(service, candidate.evidence_ids, issued)
    if candidate.id != candidate_identity(candidate):
        raise ValueError("candidate_subject_mismatch")
    change = next((item for item in changed_files if candidate.affected_path == item["file_path"]), None)
    if change is None:
        raise ValueError("candidate_outside_changed_files")
    anchor_path = (change.get("old_file_path") or candidate.affected_path
                   if change.get("change_type") == "deleted" else candidate.affected_path)
    if not any(ref.path == anchor_path
               and ref.start_line <= candidate.line <= ref.end_line for ref in refs):
        raise ValueError("candidate_anchor_not_read")
    return refs


def comparison_candidates(candidate, candidates):
    """A bounded comparison set is a hint, never a deduplication decision."""
    return sorted((item for item in candidates if item["id"] != candidate["id"]
        and item["affected_path"] == candidate["affected_path"]
        and abs(item["line"] - candidate["line"]) <= 20),
        key=lambda item: (abs(item["line"] - candidate["line"]), item["id"]))[:8]


def finding_severity(finding):
    return finding.verification.severity or finding.candidate.severity


def finding_attribution(finding):
    """已核验的归因是最终判断；候选归因只是发现阶段的初始假设。"""
    return finding.verification.attribution


def aggregate_findings(findings):
    """Only form cliques of independently confirmed, reciprocal equivalence claims."""
    remaining = {item.id: item for item in findings}
    aggregated = []

    def equivalent(left, right):
        return (right.id in left.verification.equivalent_candidate_ids
            and left.id in right.verification.equivalent_candidate_ids
            and left.verification.severity is not None
            and right.verification.severity is not None
            and finding_severity(left) == finding_severity(right)
            and finding_attribution(left) == finding_attribution(right)
            and left.candidate.affected_path == right.candidate.affected_path
            and abs(left.candidate.line - right.candidate.line) <= 20)

    while remaining:
        representative = remaining.pop(min(remaining))
        members = [representative]
        for identity in sorted(remaining):
            item = remaining[identity]
            if all(equivalent(member, item) for member in members):
                members.append(remaining.pop(identity))
        aggregated.append(representative.model_copy(update={
            "related_candidate_ids": [member.id for member in members[1:]]}))
    return aggregated


def verify_finding(candidate, result, service, changed_files, related_candidates=()):
    if result.get("status") != "completed":
        raise ValueError("verification_not_completed")
    output = VerificationOutput.model_validate(result["output"])
    refs = (source_refs(service, output.evidence_ids, result["evidence_ids"])
            if output.evidence_ids else [])
    if output.counterevidence_ids:
        source_refs(service, output.counterevidence_ids, result["evidence_ids"])
    decision = output.decision
    if decision == "refuted" and not (refs or output.counterevidence_ids):
        raise ValueError("refutation_source_missing")
    if decision == "confirmed":
        # 候选归因只是初始假设；核验方基于 BASE/HEAD 证据给出最终归因。
        if output.attribution == "uncertain":
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
        peers = {item["id"]: item for item in related_candidates}
        if len(peers) != len(related_candidates) or candidate.id in peers:
            raise ValueError("invalid_equivalence_scope")
        for identity in output.equivalent_candidate_ids:
            peer = peers.get(identity)
            if (peer is None or peer["affected_path"] != candidate.affected_path
                    or abs(peer["line"] - candidate.line) > 20 or not any(
                    ref.path == peer["affected_path"] and ref.side == "head"
                    and ref.start_line <= peer["line"] <= ref.end_line for ref in refs)):
                raise ValueError("equivalence_subject_not_read")
        if output.equivalent_candidate_ids and not any(
                ref.side == "head" and ref.path == candidate.affected_path
                and ref.start_line <= candidate.line <= ref.end_line for ref in refs):
            raise ValueError("equivalence_anchor_not_read")
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
            evidence_ids=sorted(set(output.evidence_ids)), inline_line=inline,
            related_candidate_ids=[])
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
