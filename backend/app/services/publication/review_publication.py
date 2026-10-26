"""Revalidate saved graph findings and immutable evidence before publication."""

from app.review.evidence import EvidenceArchive
from app.review.findings import candidate_gate, verify_finding
from app.services.review.artifact_store import LocalArtifactStore


def validated_findings(task, artifacts=None):
    report = task.review_report
    if report is None:
        return [], []
    expected = report.snapshot
    if (expected.review_id != task.id or expected.head_sha != task.snapshot.head_sha
            or expected.base_sha != task.snapshot.effective_base_sha
            or expected.repository != f"{task.pr.owner}/{task.pr.repo}"
            or expected.pr_number != task.pr.number):
        return [], [(finding.id, "snapshot_mismatch") for finding in report.findings]
    archive = EvidenceArchive(snapshot=expected, artifacts=artifacts or LocalArtifactStore())
    candidates = {candidate.id: candidate for candidate in report.candidates}
    decisions = {decision.candidate_id: decision for decision in report.decisions}
    results = {result.id: result.model_dump() for result in report.verification_results
               if result.status == "completed"}
    discovery = [result.model_dump() for result in report.discovery_results]
    changed = [item.model_dump(mode="json") for item in task.changed_files]
    if task.configuration.options.scope.mode == "selected":
        selected_paths = set(task.configuration.options.scope.primary_files)
        changed = [item for item in changed if item["file_path"] in selected_paths]
    selected, withheld, seen = [], [], set()
    for finding in report.findings:
        if finding.id in seen:
            withheld.append((finding.id, "duplicate_finding_conflict"))
            selected = [item for item in selected if item.id != finding.id]
            continue
        seen.add(finding.id)
        try:
            candidate = candidates[finding.id]
            if (candidate != finding.candidate or decisions[finding.id] != finding.verification
                    or finding.verification.decision != "confirmed"):
                raise ValueError("finding_subject_mismatch")
            candidate_gate(candidate, archive, discovery, changed)
            decision, rebuilt = verify_finding(candidate, results[finding.id], archive, changed)
            if rebuilt != finding or decision != finding.verification:
                raise ValueError("finding_subject_mismatch")
            selected.append(finding)
        except (KeyError, ValueError, OSError):
            withheld.append((finding.id, "publication_evidence_invalid"))
    return selected, sorted(set(withheld))
