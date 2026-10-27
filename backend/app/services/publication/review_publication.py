"""Revalidate saved graph findings and immutable evidence before publication."""

from app.review.evidence import EvidenceArchive
from app.review.findings import aggregate_findings, candidate_gate, comparison_candidates, verify_finding
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
    confirmed = {item.candidate_id for item in report.decisions if item.decision == "confirmed"}
    if len(candidates) != len(report.candidates):
        return [], [(identity, "duplicate_candidate_conflict") for identity in sorted(confirmed or candidates)]
    if len(decisions) != len(report.decisions):
        return [], [(identity, "duplicate_decision_conflict") for identity in sorted(confirmed or candidates)]
    if not confirmed <= set(candidates):
        return [], [(identity, "confirmed_candidate_missing") for identity in sorted(confirmed - set(candidates))]
    raw_candidates = []
    for candidate in report.candidates:
        try:
            candidate_gate(candidate, archive, discovery, changed)
            raw_candidates.append(candidate.model_dump())
        except (ValueError, OSError):
            if candidate.id in confirmed:
                return [], [(candidate.id, "publication_evidence_invalid")]
    member_ids = [identity for finding in report.findings
                  for identity in (finding.id, *finding.related_candidate_ids)]
    if len(member_ids) != len(set(member_ids)) or set(member_ids) != confirmed:
        missing = confirmed - set(member_ids)
        extra = set(member_ids) - confirmed
        for identity in sorted(missing):
            withheld.append((identity, "confirmed_finding_missing"))
        for identity in sorted(extra):
            withheld.append((identity, "nonconfirmed_finding_member"))
        if missing or extra:
            return [], sorted(set(withheld))
    for finding in report.findings:
        members = [finding.id, *finding.related_candidate_ids]
        if len(set(members)) != len(members) or set(members) & seen:
            withheld.append((finding.id, "duplicate_finding_conflict"))
            selected = [item for item in selected
                        if not {item.id, *item.related_candidate_ids} & set(members)]
            continue
        seen.update(members)
        try:
            rebuilt_members = []
            for identity in members:
                candidate = candidates[identity]
                decision, rebuilt = verify_finding(candidate, results[identity], archive, changed,
                    comparison_candidates(candidate.model_dump(), raw_candidates))
                if rebuilt is None or decision != decisions[identity]:
                    raise ValueError("finding_subject_mismatch")
                rebuilt_members.append(rebuilt)
            reconstructed = aggregate_findings(rebuilt_members)
            if reconstructed != [finding]:
                raise ValueError("finding_subject_mismatch")
            selected.append(finding)
        except (KeyError, ValueError, OSError):
            withheld.append((finding.id, "publication_evidence_invalid"))
    return selected, sorted(set(withheld))
