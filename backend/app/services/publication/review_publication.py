"""Publisher-only validation for normalized, persisted Run findings."""

from app.review.evidence import EvidenceArchive
from app.review.findings import finding_digest, verification_problem
from app.services.review.artifact_store import LocalArtifactStore


def _same_refs(left, right):
    return [item.model_dump() for item in left] == [item.model_dump() for item in right]


def _valid_sources(archive, refs):
    if not refs:
        return False
    for source in refs:
        loaded, _ = archive.read_source(source.id)
        if loaded.model_dump() != source.model_dump():
            return False
    return True


def _valid_finding(run, candidate, finding, archive):
    verification = candidate.verification
    if candidate.admission_reason:
        return "candidate_admission_invalid"
    if verification is None or verification.verdict != "confirmed":
        return "verification_not_confirmed"
    if verification_problem(verification, candidate.verification_sources, run.changed_files):
        return "verification_admission_invalid"
    if verification.attribution not in {"introduced", "exposed"}:
        return "finding_not_publishable"
    if candidate.finding is None or candidate.finding.model_dump() != finding.model_dump():
        return "candidate_finding_mismatch"
    if (finding.candidate_id != candidate.id or finding.claim != candidate.proposal.claim
            or finding.trigger != candidate.proposal.trigger or finding.impact != candidate.proposal.impact
            or finding.severity != verification.severity or finding.attribution != verification.attribution
            or finding.author_comment != verification.author_comment
            or finding.location != candidate.proposal.location
            or not finding.publishable):
        return "finding_fact_mismatch"
    if not _same_refs(finding.sources, candidate.verification_sources):
        return "finding_source_mismatch"
    if finding.admission_digest != finding_digest(candidate, run.snapshot):
        return "finding_digest_mismatch"
    if not (_valid_sources(archive, candidate.sources)
            and _valid_sources(archive, candidate.verification_sources)
            and _valid_sources(archive, finding.sources)):
        return "publication_evidence_invalid"
    return None


def validated_findings(run, artifacts=None):
    """Return only fully reconstructed publication facts; never replay old graphs."""
    report = run.report
    if report is None:
        return [], []
    snapshot = run.snapshot
    if (snapshot.review_id != run.id or snapshot.repository != f"{run.pr.owner}/{run.pr.repo}"
            or snapshot.repository_id <= 0 or snapshot.pr_number != run.pr.number
            or snapshot.declared_base_sha != run.pr.base.sha or snapshot.head_sha != run.pr.head.sha):
        return [], [(finding.candidate_id, "snapshot_mismatch") for finding in report.findings]
    archive = EvidenceArchive(snapshot=snapshot, artifacts=artifacts or LocalArtifactStore())
    candidates = {candidate.id: candidate for candidate in report.candidates}
    if len(candidates) != len(report.candidates):
        return [], [(candidate.id, "duplicate_candidate_conflict") for candidate in report.candidates]
    selected, withheld, seen = [], [], set()
    for finding in report.findings:
        if finding.id in seen or finding.candidate_id not in candidates:
            withheld.append((finding.candidate_id, "finding_identity_conflict"))
            continue
        seen.add(finding.id)
        try:
            reason = _valid_finding(run, candidates[finding.candidate_id], finding, archive)
        except (OSError, ValueError, TypeError):
            reason = "publication_evidence_invalid"
        if reason:
            withheld.append((finding.candidate_id, reason))
        else:
            selected.append(finding)
    for candidate in report.candidates:
        if candidate.finding is not None and candidate.finding.id not in seen:
            withheld.append((candidate.id, "finding_missing_from_report"))
    return selected, sorted(set(withheld))
