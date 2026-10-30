"""Admission checks provenance and necessary fields, never semantic truth."""

from uuid import uuid4

from app.review.domain import Candidate, Finding
from app.review.planner import identity


def admit_candidate(proposal, context, tool_call_id):
    refs = context.resolve(proposal.source_refs)
    snapshot = context.provider.run.snapshot
    if any(ref.review_id != snapshot.review_id or ref.commit_sha != (
            snapshot.head_sha if ref.side == "head" else snapshot.base_sha) for ref in refs):
        raise ValueError("candidate_snapshot_mismatch")
    if proposal.location and not any(
        ref.path == proposal.location.path and ref.side == proposal.location.side
        and ref.start_line <= proposal.location.line <= ref.end_line for ref in refs
    ):
        raise ValueError("candidate_location_not_sourced")
    return Candidate(id=uuid4().hex, unit_id=context.work.unit_id,
        attempt_id=context.work.attempt_id, tool_call_id=tool_call_id,
        proposal=proposal, sources=refs)


def finding_digest(candidate, snapshot):
    return identity({"snapshot": snapshot.model_dump(), "candidate_id": candidate.id,
        "proposal": candidate.proposal.model_dump(), "verification": candidate.verification.model_dump(),
        "sources": [ref.model_dump() for ref in candidate.verification_sources]})


def verification_problem(output, refs, changed_files):
    """Return the admission failure code for a verifier output, if any."""
    problem = None
    if output.verdict in {"confirmed", "refuted"} and not refs:
        problem = "missing_decision_sources"
    if output.verdict == "confirmed":
        if (output.severity is None or not output.reason.strip()
                or not output.author_comment or not output.author_comment.strip()):
            problem = "missing_confirmed_fields"
        if output.attribution in {"introduced", "exposed"}:
            changes = {item.file_path: item for item in changed_files}
            causal = [ref for ref in refs if ref.path in changes
                      or any(item.old_file_path == ref.path for item in changed_files)]
            added = any(changes.get(ref.path) and changes[ref.path].change_type == "added"
                        and ref.side == "head" for ref in causal)
            if not added and not {"base", "head"} <= {ref.side for ref in causal}:
                problem = "missing_change_attribution_sources"
    return problem


def admit_verification(candidate, output, context):
    refs = context.resolve(output.source_refs)
    run = context.provider.run
    problem = verification_problem(output, refs, run.changed_files)
    candidate.verification = output.model_copy(update={
        "verdict": "insufficient", "severity": None, "author_comment": None,
        "reason": f"{problem}: {output.reason}"} if problem else {})
    candidate.verification_sources = refs
    candidate.admission_reason = problem
    candidate.finding = None
    if candidate.verification.verdict == "confirmed":
        candidate.finding = Finding(id=f"finding-{candidate.id}", candidate_id=candidate.id,
            claim=candidate.proposal.claim, trigger=candidate.proposal.trigger,
            impact=candidate.proposal.impact, severity=output.severity,
            attribution=output.attribution, author_comment=output.author_comment,
            location=candidate.proposal.location, sources=refs,
            publishable=output.attribution in {"introduced", "exposed"},
            admission_digest=finding_digest(candidate, run.snapshot))
    return candidate


def aggregate(candidates):
    findings, seen = [], set()
    for candidate in candidates:
        finding = candidate.finding
        if finding is None:
            continue
        key = identity([finding.claim, finding.trigger, finding.impact, finding.severity,
                        finding.attribution, finding.author_comment,
                        finding.location.model_dump() if finding.location else None,
                        sorted(ref.id for ref in finding.sources)])
        if key not in seen:
            findings.append(finding)
            seen.add(key)
    return findings
